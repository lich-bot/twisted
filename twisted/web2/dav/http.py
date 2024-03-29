##
# Copyright (c) 2005 Apple Computer, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# DRI: Wilfredo Sanchez, wsanchez@apple.com
##

"""
HTTP Utilities
"""

__all__ = [
    "ErrorResponse",
    "MultiStatusResponse",
    "ResponseQueue",
    "statusForFailure",
]

import errno

from twisted.python import log
from twisted.python.failure import Failure
from twisted.web2 import responsecode
from twisted.web2.iweb import IResponse
from twisted.web2.http import Response, HTTPError
from twisted.web2.http_headers import MimeType
from twisted.web2.stream import MemoryStream
from twisted.web2.dav import davxml

##
# Generating and tweaking responses
##

class ErrorResponse (Response):
    """
    A L{Response} object which contains a status code and a L{davxml.Error}
    element.
    Renders itself as a DAV:error XML document.
    """
    error = None

    def __init__(self, code, error):
        """
        @param code: a response code.
        @param error: an L{davxml.WebDAVElement} identifying the error, or a
            tuple C{(namespace, name)} with which to create an empty element
            denoting the error.  (The latter is useful in the case of
            preconditions ans postconditions, not all of which have defined
            XML element classes.)
        """
        if type(error) is tuple:
            xml_namespace, xml_name = error
            class EmptyError (davxml.WebDAVEmptyElement):
                namespace = xml_namespace
                name      = xml_name
            error = EmptyError()

        output = davxml.Error(error).toxml()

        Response.__init__(self, code=code, stream=output)

        self.headers.setHeader("content-type", MimeType("text", "xml"))

        self.error = error

    def __repr__(self):
        return "<%s %s %s>" % (self.__class__.__name__, self.code, self.error.sname())

class MultiStatusResponse (Response):
    """
    Multi-status L{Response} object.
    Renders itself as a DAV:multi-status XML document.
    """
    def __init__(self, xml_responses):
        """
        @param xml_responses: an interable of davxml.Response objects.
        """
        multistatus = davxml.MultiStatus(*xml_responses)
        output = multistatus.toxml()

        Response.__init__(self, code=responsecode.MULTI_STATUS,
                          stream=davxml.MultiStatus(*xml_responses).toxml())

        self.headers.setHeader("content-type", MimeType("text", "xml"))

class ResponseQueue (object):
    """
    Stores a list of (typically error) responses for use in a
    L{MultiStatusResponse}.
    """
    def __init__(self, path_basename, method, success_code):
        """
        @param path_basename: the base path for all responses to be added to the 
            queue.
            All paths for responses added to the queue must start with
            C{path_basename}, which will be stripped from the beginning of each
            path to determine the response's URI.
        @param method: the name of the method generating the queue.
        @param success_code: the response code to return is lieu of a
            L{MultiStatusResponse} if no responses are added.
        """
        self.responses         = []
        self.path_basename     = path_basename
        self.path_basename_len = len(path_basename)
        self.method            = method
        self.success_code      = success_code

    def add(self, path, what):
        """
        Add a response.
        @param path: a path, which must be a subpath of C{path_basename} as
            provided to L{__init__}.
        @param what: a status code or a L{Failure} for the given path.
        """
        assert path.startswith(self.path_basename), "%s does not start with %s" % (path, self.path_basename)

        if type(what) is int:
            code    = what
            message = responsecode.RESPONSES[code]
        elif isinstance(what, Failure):
            code    = statusForFailure(what)
            message = str(what)
        else:
            raise AssertionError("Unknown data type: %r" % (what,))

        if code > 400: # Error codes only
            log.err("Error during %s for %s: %s" % (self.method, path, message))

        uri = path[self.path_basename_len:]

        self.responses.append(
            davxml.StatusResponse(
                davxml.HRef.fromString(uri),
                davxml.Status.fromResponseCode(code),
                davxml.ResponseDescription.fromString(message)
            )
        )

    def response(self):
        """
        Generate a L{MultiStatusResponse} with the responses contained in the queue
        or, if no such responses, return the C{success_code} provided to
        L{__init__}.
        @return: the response.
        """
        if self.responses:
            return MultiStatusResponse(self.responses)
        else:
            return self.success_code

##
# Exceptions and response codes
##

def statusForFailure(failure, what=None):
    """
    @param failure: a L{Failure}.
    @param what: a decription of what was going on when the failure occurred.
        If what is not C{None}, emit a cooresponding message via L{log.err}.
    @return: a response code cooresponding to the given C{failure}.
    """
    def msg(err):
        if what is not None:
            log.msg("%s while %s" % (err, what))

    if failure.check(IOError):
        e = failure.value[0]
        if e == errno.EACCES:
            msg("Permission denied")
            return responsecode.FORBIDDEN
        elif e == errno.ENOSPC:
            msg("Out of storage space")
            return responsecode.INSUFFICIENT_STORAGE_SPACE
        elif e == errno.ENOENT:
            msg("Not found")
            return responsecode.NOT_FOUND
        else:
            failure.raiseException()
    elif failure.check(NotImplementedError):
        msg("Unimplemented error")
        return responsecode.NOT_IMPLEMENTED
    elif failure.check(HTTPError):
        code = IResponse(failure.value.response).code
        msg("%d response" % (code,))
        return code
    else:
        failure.raiseException()
