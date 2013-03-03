import functools
import httplib
import json
import logging
import mimetypes
import os.path
import re
import shutil
import tempfile
import types
import urllib
import urllib2
import urlparse

import tuf.client.updater
import tuf.conf


# TODO:
# - document design decisions: e.g. could pip recognize multiple TUF client metadata?
# - failsafe: if TUF fails, offer option to unsafely resort back to urllib/urllib2?
# - review security issues resulting from regular expressions (e.g. complexity attacks)
# - warn user when TUF is used without any configuration
# - override other default (e.g. HTTPS) urllib2 handlers


################################ GLOBAL CLASSES ################################


class URLMatchesNoPattern( Exception ):
    """We throw this to indicate that the URL matches no user-specified
    regular expression pattern."""

    pass


class Logger( object ):
    __logger = logging.getLogger( "tuf.interposition" )

    @staticmethod
    def critical( message ):
        Logger.__logger.critical( message )
        Logger.exception( message )

    @staticmethod
    def exception( message ):
        Logger.__logger.exception( message )

    @staticmethod
    def warn( message ):
        Logger.__logger.warn( message )
        Logger.exception( message )


class Configuration( object ):
    def __init__(
        self,
        hostname,
        repository_directory,
        repository_mirrors,
        target_paths
    ):
        self.hostname = hostname
        self.repository_directory = repository_directory
        self.repository_mirrors = repository_mirrors
        self.target_paths = target_paths
        self.tempdir = tempfile.mkdtemp()

    @staticmethod
    def load_from_json(
        hostname,
        configuration,
        parent_repository_directory = None
    ):

        INVALID_PARENT_REPOSITORY_DIRECTORY = "Ignoring invalid " + \
            "parent_repository_directory for {hostname}!"
        # An "identity" capture from source URL to target URL
        WILD_TARGET_PATH = { "(.*)": "{0}" }

        repository_directory = configuration[ "repository_directory" ]
        if parent_repository_directory is not None:
            parent_repository_directory = \
                os.path.abspath( parent_repository_directory )
            if os.path.isdir( parent_repository_directory ):
                repository_directory = os.path.join(
                    parent_repository_directory,
                    repository_directory
                )
            else:
                Logger.warn(
                    INVALID_PARENT_REPOSITORY_DIRECTORY.format(
                        hostname = hostname
                    )
                )

        repository_mirrors = configuration[ "repository_mirrors" ]

        # Within a hostname, we match URLs with this list of regular expressions,
        # which tell us to map from a source URL to a target URL.
        # If there are multiple regular expressions which match a source URL,
        # the order of appearance will be used to resolve ambiguity.
        target_paths = \
            configuration.get( "target_paths", [ WILD_TARGET_PATH  ] )

        # target_paths: [ target_path, ... ]
        assert isinstance( target_paths, types.ListType )
        for target_path in target_paths:
            # target_path: { "regex_with_groups", "target_with_group_captures" }
            # e.g. { ".*(/some/directory)/$", "{0}/index.html" }
            assert isinstance( target_path, types.DictType )
            assert len( target_path ) == 1

        return Configuration(
            hostname,
            repository_directory,
            repository_mirrors,
            target_paths
        )


class Updater( object ):
    """
    You can think of Updater as being a factory of Updaters;
    given a Configuration, it will build and store an Updater
    which you can get and use later.
    """

    # A private collection of Updaters;
    # hostname: str -> updater: Updater
    __updaters = {}

    def __init__( self, configuration ):
        self.configuration = configuration

        # must switch context before instantiating updater
        # because updater depends on some module (tuf.conf) variables
        self.switch_context()
        self.updater = tuf.client.updater.Updater(
            self.configuration.hostname,
            self.configuration.repository_mirrors
        )

    @staticmethod
    def build_updater( configuration ):
        assert isinstance( configuration, Configuration )
        assert configuration.hostname not in Updater.__updaters

        Updater.__updaters[ configuration.hostname ] = Updater( configuration )

    def download_target( self, target_filepath ):
        """Downloads target with TUF as a side effect."""

        # download file into a temporary directory shared over runtime
        destination_directory = self.configuration.tempdir
        filename = os.path.join( destination_directory, target_filepath )

        # switch TUF context
        self.switch_context()
        # update TUF client repository metadata
        self.updater.refresh()

        # then, update target at filepath
        targets = [ self.updater.target( target_filepath ) ]

        # TODO: targets are always updated if destination directory is new, right?
        updated_targets = self.updater.updated_targets(
            targets, destination_directory
        )

        for updated_target in updated_targets:
            self.updater.download_target(
                updated_target, destination_directory
            )

        return destination_directory, filename

    # TODO: decide prudent course of action in case of failure
    def get_target_filepath( self, source_url ):
        """Given source->target map,
        figure out what TUF *should* download given a URL."""

        ERROR_MESSAGE = "Possibly invalid target_paths for " + \
            "{hostname}! TUF interposition will NOT be present for {url}"

        parsed_source_url = urlparse.urlparse( source_url )
        target_filepath = None

        try:
            # Does this source URL match any regular expression which tells us
            # how to map the source URL to a target URL understood by TUF?
            for target_path in self.configuration.target_paths:
                # target_path: { "regex_with_groups", "target_with_group_captures" }
                # e.g. { ".*(/some/directory)/$", "{0}/index.html" }
                source_path_pattern, target_path_pattern = \
                    target_path.items()[ 0 ]
                source_path_match = \
                    re.match( source_path_pattern, parsed_source_url.path )

                if source_path_match is not None:
                    target_filepath = target_path_pattern.format(
                        *source_path_match.groups()
                    )
                    # If there is more than one regular expression which
                    # matches source_url, we resolve ambiguity by order of
                    # appearance.
                    break

            # If source_url does not match any regular expression...
            if target_filepath is None:
                # ...then we raise a predictable exception.
                raise URLMatchesNoPattern( source_url )
        except:
            Logger.critical(
                ERROR_MESSAGE.format(
                    hostname = self.configuration.hostname,
                    url = source_url
                )
            )
            raise
        else:
            # TUF assumes that target_filepath does not begin with a '/'.
            target_filepath = target_filepath.lstrip( '/' )
            return target_filepath

    @staticmethod
    def get_updater( url ):
        ERROR_MESSAGE = "Could not get updater for {hostname}! " + \
            "TUF interposition will NOT be present for {url}"

        updater = None

        try:
            parsed_url = urlparse.urlparse( url )
            # TODO: enable specificity beyond hostname (e.g. include scheme, port)
            updater = Updater.__updaters.get( parsed_url.hostname )
            # This will raise an exception in case we do not recognize
            # how to transform this URL for TUF. In that case, there will be
            # no updater for this URL.
            if updater is not None:
                target_filepath = updater.get_target_filepath( url )
        except:
            Logger.critical( ERROR_MESSAGE )
            updater = None
        finally:
            return updater

    # TODO: distinguish between urllib and urllib2 contracts
    def open( self, url, data = None ):
        filename, headers = self.retrieve( url, data = data )

        # TODO: like tempfile, ensure file is deleted when closed?
        tempfile = open( filename )
        # extend tempfile with info(), getcode(), geturl()
        # http://docs.python.org/2/library/urllib.html#urllib.urlopen
        response = urllib.addinfourl(
            tempfile,
            headers,
            url,
            code = 200
        )

        return response

    # TODO: distinguish between urllib and urllib2 contracts
    def retrieve(
        self,
        url,
        filename = None,
        reporthook = None,
        data = None
    ):
        # TODO: set valid headers
        content_type, content_encoding = mimetypes.guess_type( url )
        headers = { "content-type": content_type }

        target_filepath = self.get_target_filepath( url )

        temporary_directory, temporary_filename = \
            self.download_target( target_filepath )

        if filename is None:
            # If no filename is given, use the temporary file.
            filename = temporary_filename
        else:
            # Otherwise, copy TUF-downloaded file in its own directory
            # to the location user specified.
            shutil.copy2( temporary_filename, filename )

        return filename, headers

    # TODO: thread-safety, perhaps with a context manager
    def switch_context( self ):
        # Set the local repository directory containing the metadata files.
        tuf.conf.repository_directory = \
            self.configuration.repository_directory


class FancyURLOpener( urllib.FancyURLopener ):
    # TODO: replicate complete behaviour of urllib.URLopener.open
    def open( self, fullurl, data = None ):
        updater = Updater.get_updater( fullurl )

        if updater is None:
            return urllib.FancyURLopener.open( self, fullurl, data = data )
        else:
            return updater.open( fullurl, data = data )

    # TODO: replicate complete behaviour of urllib.URLopener.retrieve
    def retrieve( self, url, filename = None, reporthook = None, data = None ):
        updater = Updater.get_updater( url )

        if updater is None:
            return urllib.FancyURLopener.retrieve(
                self,
                url,
                filename = filename,
                reporthook = reporthook,
                data = data
            )
        else:
            return updater.retrieve(
                url,
                filename = filename,
                reporthook = reporthook,
                data = data
            )


class HTTPHandler( urllib2.HTTPHandler ):
    # TODO: replicate complete behaviour of urllib.HTTPHandler.http_open
    def http_open( self, req ):
        fullurl = req.get_full_url()
        updater = Updater.get_updater( fullurl )

        if updater is None:
            return self.do_open( httplib.HTTPConnection, req )
        else:
            response = updater.open( fullurl, data = req.get_data() )
            # See urllib2.AbstractHTTPHandler.do_open
            # TODO: let DownloadMixin handle this
            response.msg = ""
            return response


############################## GLOBAL FUNCTIONS ################################


# TODO: Is parent_repository_directory a security risk? For example, would it
# allow the user to overwrite another TUF repository metadata on the filesystem?
# On the other hand, it is beyond TUF's scope to handle filesystem permissions.
def configure(
    filename = "tuf.interposition.json",
    parent_repository_directory = None
):
    """
    The optional parent_repository_directory parameter is used to specify the
    containing parent directory of the "repository_directory" specified in a
    configuration for *all* hostnames, because sometimes the absolute location
    of the "repository_directory" is only known at runtime. If you need to
    specify a different parent_repository_directory for other hostnames, simply
    call this method again with different parameters.

    Example of a TUF interposition configuration JSON object:

    {
        "hostnames": {
            "seattle.cs.washington.edu": {
                "repository_directory": "client/",
                "repository_mirrors" : {
                    "mirror1": {
                        "url_prefix": "http://seattle-tuf.cs.washington.edu",
                        "metadata_path": "metadata",
                        "targets_path": "targets",
                        "confined_target_dirs": [ "" ]
                    }
                },
                ("target_paths": [
                    { ".*/(simple/\\w+)/$": "{0}/index.html" },
                    { ".*/(packages/.+)$": "{0}" }
                ])
            }
        }
    }

    "target_paths" is optional: If you do not tell TUF to selectively match
    paths with regular expressions, TUF will work over any path under the given
    hostname. However, if you do specify it, you are then telling TUF how to
    transform a specified path into another one, and TUF will *not* recognize
    any unspecified path for the given hostname.
    """

    INVALID_TUF_CONFIGURATION = "Invalid TUF configuration for " + \
        "{hostname}! TUF interposition will NOT be present for {hostname}."
    INVALID_TUF_INTERPOSITION_JSON = "Invalid TUF configuration JSON file " + \
        "{filename}! TUF interposition will NOT be present for any host."
    NO_HOSTNAMES = "No hostnames found in TUF configuration JSON file " + \
        "{filename}! TUF interposition will NOT be present for any host."

    try:
        with open( filename ) as tuf_interposition_json:
            tuf_interpositions = json.load( tuf_interposition_json )
            hostnames = tuf_interpositions.get( "hostnames", {} )

            # TODO: more input sanity checks
            if len( hostnames ) == 0:
                Logger.warn( NO_HOSTNAMES.format( filename = filename ) )
            else:
                for hostname, configuration in hostnames.iteritems():
                    try:
                        Updater.build_updater(
                            Configuration.load_from_json(
                                hostname,
                                configuration,
                                parent_repository_directory = parent_repository_directory
                            )
                        )
                    except:
                        Logger.warn(
                            INVALID_TUF_CONFIGURATION.format(
                                hostname = hostname
                            )
                        )
    except:
        Logger.warn(
            INVALID_TUF_INTERPOSITION_JSON.format( filename = filename )
        )


def go_away():
    """Call me to restore previous urllib and urllib2 behaviour."""

    global _previous_urllib_urlopener
    global _previous_urllib2_opener

    if _previous_urllib_urlopener is not False:
        urllib._urlopener = _previous_urllib_urlopener
        _previous_urllib_urlopener = None

    if _previous_urllib2_opener is not False:
        # NOTE: slightly rude and, furthermore, fragile
        urllib2._opener = _previous_urllib2_opener
        _previous_urllib2_opener = None


def interpose():
    """Call me to have TUF interpose as urllib and urllib2."""

    global _previous_urllib_urlopener
    global _previous_urllib2_opener

    if _previous_urllib_urlopener is False:
        _previous_urllib_urlopener = urllib._urlopener
        # http://docs.python.org/2/library/urllib.html#urllib._urlopener
        urllib._urlopener = FancyURLOpener()

    if _previous_urllib2_opener is False:
        # NOTE: slightly rude and, furthermore, fragile
        _previous_urllib2_opener = urllib2._opener
        # http://docs.python.org/2/library/urllib2.html#urllib2.build_opener
        # http://docs.python.org/2/library/urllib2.html#urllib2.install_opener
        urllib2.install_opener( urllib2.build_opener( HTTPHandler ) )


def open_url( method ):
    """Decorate a caller instance method of the form method( self, url, ... )
    with this decorator in order to provide it with TUF security."""

    @functools.wraps( method )
    def wrapper( self, *args, **kwargs ):
        # TODO: Ensure that the first argument to method is a URL.
        url = args[ 0 ]
        data = kwargs.get( "data" )
        updater = Updater.get_updater( url )

        # If TUF has not been configured for this URL...
        if updater is None:
            # ...then revert to default behaviour.
            return method( self, *args, **kwargs )
        else:
            # ...otherwise, use TUF to get this document.
            return updater.open( url, data = data )

    return wrapper


############################## GLOBAL VARIABLES ################################


# We use False as a sentinal value.
_previous_urllib_urlopener = False
_previous_urllib2_opener = False