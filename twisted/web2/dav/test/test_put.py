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

import os
import filecmp

from twisted.internet.defer import Deferred
from twisted.web2 import responsecode
from twisted.web2.iweb import IResponse
from twisted.web2.stream import FileStream

import twisted.web2.dav.test.util
from twisted.web2.dav.test.util import SimpleRequest, serialize
from twisted.web2.dav.fileop import rmdir

class PUT(twisted.web2.dav.test.util.TestCase):
    """
    PUT request
    """
    def test_PUT_simple(self):
        """
        PUT request
        """
        dst_path = os.path.join(self.docroot, "dst")

        dst_uri = "/put_simple"

        def check_result(response, path):
            response = IResponse(response)

            if response.code not in (
                responsecode.CREATED,
                responsecode.NO_CONTENT
            ):
                self.fail("PUT failed: %s" % (response.code,))

            if not filecmp.cmp(path, dst_path):
                self.fail("PUT failed to preserve data for file %s." % (path,))

            etag = response.headers.getHeader("etag")
            if not etag:
                self.fail("No etag header in PUT response %r." % (response,))

        #
        # We need to serialize these request & test iterations because they can
        # interfere with each other.
        #
        def work():
            for path, uri in self.list():
                # Avoid infinite loop
                if path.startswith(dst_path): continue
    
                # Can't really PUT something you can't read
                if not os.path.isfile(path): continue
    
                def do_test(response): check_result(response, path)
    
                request = SimpleRequest(self.site, "PUT", uri)
                request.stream = FileStream(file(path, "rb"))
    
                yield (request, do_test, dst_path)

        return serialize(self.send, work())

    def test_PUT_again(self):
        """
        PUT on existing resource with If-None-Match header
        """
        dst_path = os.path.join(self.docroot, "dst")
        dst_uri = "/put_again"

        def work():
            for code in (
                responsecode.CREATED,
                responsecode.PRECONDITION_FAILED,
                responsecode.NO_CONTENT,
                responsecode.PRECONDITION_FAILED,
                responsecode.NO_CONTENT,
                responsecode.CREATED,
            ):
                def check_result(response, code=code):
                    response = IResponse(response)

                    if response.code != code:
                        self.fail("Incorrect response code for PUT (%s != %s)"
                                  % (response.code, code))

                request = SimpleRequest(self.site, "PUT", dst_uri)
                request.stream = FileStream(file(__file__, "rb"))
    
                if code == responsecode.CREATED:
                    if os.path.isfile(dst_path):
                        os.remove(dst_path)
                    request.headers.setHeader("if-none-match", ("*",))
                elif code == responsecode.PRECONDITION_FAILED:
                    request.headers.setHeader("if-none-match", ("*",))
    
                yield (request, check_result, dst_path)

        return serialize(self.send, work())

    def test_PUT_no_parent(self):
        """
        PUT with no parent
        """
        dst_path = os.path.join(self.docroot, "put", "no", "parent")
        dst_uri = "/put/no/parent"

        def check_result(response):
            response = IResponse(response)

            if response.code != responsecode.CONFLICT:
                self.fail("Incorrect response code for PUT with no parent (%s != %s)"
                          % (response.code, responsecode.CONFLICT))

        request = SimpleRequest(self.site, "PUT", dst_uri)
        request.stream = FileStream(file(__file__, "rb"))

        return self.send(request, check_result, dst_path)
