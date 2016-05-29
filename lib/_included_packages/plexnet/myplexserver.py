import plexconnection
import plexserver
import plexresource
import plexservermanager
import myplexaccount


class MyPlexServer(plexserver.PlexServerBase):
    def __init__(self):
        plexserver.PlexServerBase.__init__(self)
        self.uuid = 'myplex'
        self.name = 'plex.tv'

        conn = plexconnection.PlexConnection(plexresource.ResourceConnection.SOURCE_MYPLEX, "https://plex.tv", False, None)
        self.connections.append(conn)
        self.activeConnection = conn

    def getToken(self):
        return myplexaccount.ACCOUNT.authToken

    def buildUrl(self, path, includeToken=False):
        if "://node.plexapp.com" in path:
            # Locate the best fit server that supports channels, otherwise we'll
            # continue to use the node urls. Service code between the node and
            # PMS differs sometimes, so it's a toss up which one is actually
            # more accurate. Either way, we try to offload work from the node.

            server = plexservermanager.MANAGER.getChannelServer()
            if server:
                url = server.swizzleUrl(path, includeToken)
                if url:
                    return url

        return plexserver.PlexServerBase.buildUrl(self, path, includeToken)
