import util
import plexapp
import captions
import http
import mediadecisionengine


class PlexPlayer(object):
    def __init__(self, item, seekValue=0):
        self.item = item
        self.seekValue = seekValue
        self.choice = None  # MediaDecisionEngine().chooseMedia(item)
        if self.choice:
            self.media = self.choice.media

    def build(self, directPlay=None, directStream=True, currentPartIndex=None):
        isForced = bool(directPlay)
        if isForced:
            util.LOG(directPlay and "Forced Direct Play" or "Forced Transcode; allowDirectStream={0}".format(directStream))

        directPlay = directPlay or self.choice.isDirectPlayable
        server = self.item.getServer()

        # A lot of our content metadata is independent of the direct play decision.
        # Add that first.

        obj = util.AttributeDict()
        obj.duration = self.media.duration.asInt()

        videoRes = self.media.getVideoResolution()
        obj.fullHD = videoRes >= 1080
        obj.streamQualities = (videoRes >= 480 and plexapp.INTERFACE.getGlobal("IsHD")) and ["HD"] or ["SD"]

        frameRate = self.media.videoFrameRate or "24p"
        if frameRate == "24p":
            obj.frameRate = 24
        elif frameRate == "NTSC":
            obj.frameRate = 30

        # Add soft subtitle info
        if self.choice.subtitleDecision == self.choice.SUBTITLES_SOFT_ANY:
            obj.subtitleUrl = server.buildUrl(self.choice.subtitleStream.getSubtitlePath(), True)
        elif self.choice.subtitleDecision == self.choice.SUBTITLES_SOFT_DP:
            obj.subtitleConfig = {'TrackName': "mkv/" + str(self.choice.subtitleStream.index.asInt() + 1)}

        # Create one content metadata object for each part and store them as a
        # linked list. We probably want a doubly linked list, except that it
        # becomes a circular reference nuisance, so we make the current item the
        # base object and singly link in each direction from there.

        baseObj = obj
        prevObj = None
        startOffset = 0

        startPartIndex = currentPartIndex or 0
        for partIndex in range(startPartIndex, len(self.media.parts)):
            isCurrentPart = (currentPartIndex is not None and partIndex == currentPartIndex)
            partObj = util.AttributeDict()
            partObj.update(baseObj)

            partObj.live = False
            partObj.partIndex = partIndex
            partObj.startOffset = startOffset

            part = self.media.parts[partIndex]
            if part.isIndexed():
                partObj.sdBifPath = part.getIndexPath("sd")
                partObj.hdBifPath = part.getIndexPath("hd")

            # We have to evaluate every part before playback. Normally we'd expect
            # all parts to be identical, but in reality they can be different.

            if partIndex > 0 and (not isForced and directPlay or not isCurrentPart):
                choice = mediadecisionengine.MediaDecisionEngine().evaluateMediaVideo(self.item, self.media, partIndex)
                canDirectPlay = (choice.isDirectPlayable is True)
            else:
                canDirectPlay = directPlay

            if canDirectPlay:
                partObj = self.buildDirectPlay(partObj, partIndex)
            else:
                transcodeServer = self.item.getTranscodeServer(True, "video")
                if transcodeServer is None:
                    return None
                partObj = self.buildTranscode(transcodeServer, partObj, partIndex, directStream, isCurrentPart)

            # Set up our linked list references. If we couldn't build an actual
            # object: fail fast. Otherwise, see if we're at our start offset
            # yet in order to decide if we need to link forwards or backwards.
            # We also need to account for parts missing a duration, by verifying
            # the prevObj is None or if the startOffset has incremented.

            if partObj is None:
                obj = None
                break
            elif prevObj is None or (startOffset > 0 and int(self.seekValue / 1000) >= startOffset):
                obj = partObj
                partObj.prevObj = prevObj
            elif prevObj is not None:
                prevObj.nextPart = partObj

            startOffset = startOffset + int(part.duration.asInt() / 1000)

            prevObj = partObj

        # Only set PlayStart for the initial part, and adjust for the part's offset
        if obj is not None:
            if obj.live:
                # Start the stream at the end. Per Roku, this can be achieved using
                # a number higher than the duration. Using the current time should
                # ensure it's definitely high enough.

                obj.playStart = util.now() + 1800
            else:
                obj.playStart = int(self.seekValue / 1000) - obj.startOffset

        self.metadata = obj

        util.LOG("Constructed video item for playback: {0}".format(obj))

        return self.metadata

    def buildTranscodeHls(self, obj):
        settings = plexapp.INTERFACE
        obj.streamFormat = "hls"
        obj.streamBitrates = [0]
        obj.switchingStrategy = "no-adaptation"

        builder = http.HttpRequest(obj.transcodeServer.buildUrl("/video/:/transcode/universal/start.m3u8", True))
        builder.extras = []
        builder.addParam("protocol", "hls")

        if self.choice.subtitleDecision == self.choice.SUBTITLES_SOFT_ANY:
            builder.addParam("skipSubtitles", "1")
        elif self.choice.hasBurnedInSubtitles is True:
            captionSize = captions.CAPTIONS.getBurnedSize()
            if captionSize is not None:
                builder.addParam("subtitleSize", captionSize)

        # Augment the server's profile for things that depend on the Roku's configuration.
        if settings.supportsAudioStream("ac3", 6):
            builder.extras.append("append-transcode-target-audio-codec(type=videoProfile&context=streaming&protocol=hls&audioCodec=ac3)")
            builder.extras.append("add-direct-play-profile(type=videoProfile&container=matroska&videoCodec=*&audioCodec=ac3)")

        return builder

    def buildTranscodeMkv(self, obj):
        settings = plexapp.INTERFACE
        obj.streamFormat = "mkv"
        obj.streamBitrates = [0]

        builder = http.HttpRequest(obj.transcodeServer.buildUrl("/video/:/transcode/universal/start.mkv", True))
        builder.extras = []
        builder.addParam("protocol", "http")
        builder.addParam("copyts", "1")

        obj.subtitleUrl = None
        if self.choice.subtitleDecision == self.choice.SUBTITLES_BURN:
            builder.addParam("subtitles", "burn")
            captionSize = captions.CAPTIONS.getBurnedSize()
            if captionSize is not None:
                builder.addParam("subtitleSize", captionSize)

        else:
            # TODO(rob): can we safely assume the id will also be 3 (one based index).
            # If not, we will have to get tricky and select the subtitle stream after
            # video playback starts via roCaptionRenderer: GetSubtitleTracks() and
            # ChangeSubtitleTrack()

            obj.subtitleConfig = {'TrackName': "mkv/3"}

            # Allow text conversion of subtitles if we only burn image formats
            if settings.getPreference("burn_subtitles") == "image":
                builder.addParam("advancedSubtitles", "text")

            builder.addParam("subtitles", "auto")

        # Augment the server's profile for things that depend on the Roku's configuration.
        if settings.supportsSurroundSound():
            if self.choice.audioStream is not None:
                numChannels = self.choice.audioStream.channels.asInt(6)
            else:
                numChannels = 6

            for codec in ("ac3", "eac3", "dca"):
                if settings.supportsAudioStream(codec, numChannels):
                    builder.extras.append("append-transcode-target-audio-codec(type=videoProfile&context=streaming&protocol=http&audioCodec=" + codec + ")")
                    builder.extras.append("add-direct-play-profile(type=videoProfile&container=matroska&videoCodec=*&audioCodec=" + codec + ")")
                    if codec == "dca":
                        builder.extras.append(
                            "add-limitation(scope=videoAudioCodec&scopeName=dca&type=upperBound&name=audio.channels&value=6&isRequired=false)"
                        )

        # AAC sample rate cannot be less than 22050hz (HLS is capable).
        if self.choice.audioStream is not None and self.choice.audioStream.samplingRate.asInt(22050) < 22050:
            builder.extras.append("add-limitation(scope=videoAudioCodec&scopeName=aac&type=lowerBound&name=audio.samplingRate&value=22050&isRequired=false)")

        # HEVC and VP9 support!
        if settings.getGlobal("hevcSupport"):
            builder.extras.append("append-transcode-target-codec(type=videoProfile&context=streaming&protocol=http&videoCodec=hevc)")

        if settings.getGlobal("vp9Support"):
            builder.extras.append("append-transcode-target-codec(type=videoProfile&context=streaming&protocol=http&videoCodec=vp9)")

        return builder

    def buildDirectPlay(self, obj, partIndex):
        part = self.media.parts[partIndex]
        server = self.item.getServer()

        # Check if we should include our token or not for this request
        obj.isRequestToServer = server.isRequestToServer(server.buildUrl(part.getAbsolutePath("key")))
        obj.streamUrls = [server.buildUrl(part.getAbsolutePath("key"), obj.isRequestToServer)]
        obj.token = obj.isRequestToServer and server.getToken() or None
        if self.media.protocol == "hls":
            obj.streamFormat = "hls"
            obj.switchingStrategy = "full-adaptation"
            obj.live = self.isLiveHLS(obj.streamUrls[0], self.media.indirectHeaders)
        else:
            obj.streamFormat = self.media.container or 'mp4'
            if obj.streamFormat == "mov" or obj.streamFormat == "m4v":
                obj.streamFormat = "mp4"

        obj.streamBitrates = [self.media.bitrate.asInt()]
        obj.isTranscoded = False

        if self.choice.audioStream is not None:
            obj.audioLanguageSelected = self.choice.audioStream.languageCode

        return obj

    def hasMoreParts(self):
        return (self.metadata is not None and self.metadata.nextPart is not None)

    def goToNextPart(self):
        oldPart = self.metadata
        if oldPart is None:
            return

        newPart = oldPart.nextPart
        if newPart is None:
            return

        newPart.prevPart = oldPart
        oldPart.nextPart = None
        self.metadata = newPart

        util.LOG("Next part set for playback: {0}".format(self.metadata))

    def getBifUrl(self, offset):
        server = self.item.getServer()
        if server is not None and self.metadata is not None:
            bifUrl = self.metadata.hdBifPath or self.metadata.sdBifPath
            if bifUrl is not None:
                return server.buildUrl('{0}/{1}'.format(bifUrl, offset), True)

        return None

    def buildTranscode(self, server, obj, partIndex, directStream, isCurrentPart):
        settings = plexapp.INTERFACE
        obj.transcodeServer = server
        obj.isTranscoded = True

        if server.supportsFeature("mkv_transcode") and settings.getPreference("transcode_format") == "mkv":
            builder = self.buildTranscodeMkv(obj)
        else:
            builder = self.buildTranscodeHls(obj)

        import myplexserver
        if self.item.getServer() == myplexserver.MyPlexServer():
            path = server.swizzleUrl(self.item.getAbsolutePath("key"))
        else:
            path = self.item.getAbsolutePath("key")

        builder.addParam("path", path)

        part = self.media.parts[partIndex]
        seekOffset = int(self.seekValue / 1000)

        # Disabled for HLS due to a Roku bug plexinc/roku-client-issues#776
        if obj.streamFormat == "mkv":
            # Trust our seekOffset for this part if it's the current part (now playing) or
            # the seekOffset is within the time frame. We have to trust the current part
            # as we may have to rebuild the transcode when seeking, and not all parts
            # have a valid duration.

            if isCurrentPart or len(self.media.parts) <= 1 or (
                seekOffset >= obj.startOffset and seekOffset <= obj.startOffset + int(part.duration.asInt() / 1000)
            ):
                startOffset = seekOffset - obj.startOffset

                # Avoid a perfect storm of PMS and Roku quirks. If we pass an offset to
                # the transcoder,: it'll start transcoding from that point. But if
                # we try to start a few seconds into the video, the Roku seems to want
                # to grab the first segment. The first segment doesn't exist, so PMS
                # returns a 404 (but only if the offset is <= 12s, otherwise it returns
                # a blank segment). If the Roku gets a 404 for the first segment,:
                # it'll fail. So, if we're going to start playing from less than 12
                # seconds, don't bother telling the transcoder. It's not worth the
                # potential failure, let it transcode from the start so that the first
                # segment will always exist.

                if startOffset <= 12:
                    startOffset = 0
            else:
                startOffset = 0

            builder.addParam("offset", str(startOffset))

        builder.addParam("session", settings.getGlobal("clientIdentifier"))
        builder.addParam("directStream", directStream and "1" or "0")
        builder.addParam("directPlay", "0")

        qualityIndex = settings.getQualityIndex(self.item.getQualityType(server))
        builder.addParam("videoQuality", settings.getGlobal("transcodeVideoQualities")[qualityIndex])
        builder.addParam("videoResolution", settings.getGlobal("transcodeVideoResolutions")[qualityIndex])
        builder.addParam("maxVideoBitrate", settings.getGlobal("transcodeVideoBitrates")[qualityIndex])

        if self.media.mediaIndex is not None:
            builder.addParam("mediaIndex", str(self.media.mediaIndex))

        builder.addParam("partIndex", str(partIndex))

        # Augment the server's profile for things that depend on the Roku's configuration.
        if settings.getPreference("h264_level", "auto") != "auto":
            builder.extras.append(
                "add-limitation(scope=videoCodec&scopeName=h264&type=upperBound&name=video.level&value={0}&isRequired=true)".format(
                    settings.getPreference("h264_level")
                )
            )

        if not settings.getGlobal("supports1080p60") and settings.getGlobal("transcodeVideoResolutions")[qualityIndex][0] >= 1920:
            builder.extras.append("add-limitation(scope=videoCodec&scopeName=h264&type=upperBound&name=video.frameRate&value=30&isRequired=false)")

        if builder.extras:
            builder.addParam("X-Plex-Client-Profile-Extra", '+'.join(builder.extras))

        obj.streamUrls = [builder.getUrl()]

        return obj
