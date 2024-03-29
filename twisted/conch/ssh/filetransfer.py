# -*- test-case-name: twisted.conch.test.test_filetransfer -*-
#
# Copyright (c) 2001-2004 Twisted Matrix Laboratories.
# See LICENSE for details.

#

import struct, errno

from twisted.internet import defer, protocol
from twisted.python import failure, log

from common import NS, getNS
from twisted.conch.interfaces import ISFTPServer, ISFTPFile

from zope import interface

class FileTransferBase(protocol.Protocol):

    versions = (3, )

    packetTypes = {}

    def __init__(self):
        self.buf = ''
        self.otherVersion = None # this gets set

    def sendPacket(self, kind, data):
        self.transport.write(struct.pack('!LB', len(data)+1, kind) + data)

    def dataReceived(self, data):
        self.buf += data
        while len(self.buf) > 5:
            length, kind = struct.unpack('!LB', self.buf[:5])
            if len(self.buf) < 4 + length:
                return
            data, self.buf = self.buf[5:4+length], self.buf[4+length:]
            packetType = self.packetTypes.get(kind, None)
            if not packetType:
                log.msg('no packet type for', kind)
                continue
            f = getattr(self, 'packet_%s' % packetType, None)
            if not f:
                log.msg('not implemented: %s' % packetType)
                log.msg(repr(data[4:]))
                reqId, = struct.unpack('!L', data[:4])
                self._sendStatus(reqId, FX_OP_UNSUPPORTED,
                                 "don't understand %s" % packetType)
                #XXX not implemented
                continue
            try:
                f(data)
            except Exception, e:
                log.err(e)
                continue
                reqId ,= struct.unpack('!L', data[:4])
                self._ebStatus(failure.Failure(e), reqId)

    def _parseAttributes(self, data):
        flags ,= struct.unpack('!L', data[:4])
        attrs = {}
        data = data[4:]
        if flags & FILEXFER_ATTR_SIZE == FILEXFER_ATTR_SIZE:
            size ,= struct.unpack('!Q', data[:8])
            attrs['size'] = size
            data = data[8:]
        if flags & FILEXFER_ATTR_OWNERGROUP == FILEXFER_ATTR_OWNERGROUP:
            uid, gid = struct.unpack('!2L', data[:8])
            attrs['uid'] = uid
            attrs['gid'] = gid
            data = data[8:]
        if flags & FILEXFER_ATTR_PERMISSIONS == FILEXFER_ATTR_PERMISSIONS:
            perms ,= struct.unpack('!L', data[:4])
            attrs['permissions'] = perms
            data = data[4:]
        if flags & FILEXFER_ATTR_ACMODTIME == FILEXFER_ATTR_ACMODTIME:
            atime, mtime = struct.unpack('!2L', data[:8])
            attrs['atime'] = atime
            attrs['mtime'] = mtime
            data = data[8:]
        if flags & FILEXFER_ATTR_EXTENDED == FILEXFER_ATTR_EXTENDED:
            extended_count ,= struct.unpack('!L', data[4:])
            data = data[4:]
            for i in xrange(extended_count):
                extended_type, data = getNS(data)
                extended_data, data = getNS(data)
                attrs['ext_%s' % extended_type] = extended_data
        return attrs, data

    def _packAttributes(self, attrs):
        flags = 0
        data = ''
        if 'size' in attrs:
            data += struct.pack('!Q', attrs['size'])
            flags |= FILEXFER_ATTR_SIZE
        if 'uid' in attrs and 'gid' in attrs:
            data += struct.pack('!2L', attrs['uid'], attrs['gid'])
            flags |= FILEXFER_ATTR_OWNERGROUP
        if 'permissions' in attrs:
            data += struct.pack('!L', attrs['permissions'])
            flags |= FILEXFER_ATTR_PERMISSIONS
        if 'atime' in attrs and 'mtime' in attrs:
            data += struct.pack('!2L', attrs['atime'], attrs['mtime'])
            flags |= FILEXFER_ATTR_ACMODTIME
        extended = []
        for k in attrs:
            if k.startswith('ext_'):
                ext_type = NS(k[4:])
                ext_data = NS(attrs[k])
                extended.append(ext_type+ext_data)
        if extended:
            data += struct.pack('!L', len(extended))
            data += ''.join(extended)
            flags |= FILEXFER_ATTR_EXTENDED
        return struct.pack('!L', flags) + data

class FileTransferServer(FileTransferBase):

    def __init__(self, data=None, avatar=None):
        FileTransferBase.__init__(self)
        self.client = ISFTPServer(avatar) # yay interfaces
        self.openFiles = {}
        self.openDirs = {}

    def packet_INIT(self, data):
        version ,= struct.unpack('!L', data[:4])
        self.version = min(list(self.versions) + [version])
        data = data[4:]
        ext = {}
        while data:
            ext_name, data = getNS(data)
            ext_data, data = getNS(data)
            ext[ext_name] = ext_data
        our_ext = self.client.gotVersion(version, ext)
        our_ext_data = ""
        for (k,v) in our_ext.items():
            our_ext_data += NS(k) + NS(v)
        self.sendPacket(FXP_VERSION, struct.pack('!L', self.version) + \
                                     our_ext_data)

    def packet_OPEN(self, data):
        requestId = data[:4]
        data = data[4:]
        filename, data = getNS(data)
        flags ,= struct.unpack('!L', data[:4])
        data = data[4:]
        attrs, data = self._parseAttributes(data)
        assert data == '', 'still have data in OPEN: %s' % repr(data)
        d = defer.maybeDeferred(self.client.openFile, filename, flags, attrs)
        d.addCallback(self._cbOpenFile, requestId)
        d.addErrback(self._ebStatus, requestId, "open failed")

    def _cbOpenFile(self, fileObj, requestId):
        fileId = str(hash(fileObj))
        if fileId in self.openFiles:
            raise KeyError, 'id already open'
        self.openFiles[fileId] = fileObj
        self.sendPacket(FXP_HANDLE, requestId + NS(fileId))

    def packet_CLOSE(self, data):
        requestId = data[:4]
        data = data[4:]
        handle, data = getNS(data)
        assert data == '', 'still have data in CLOSE: %s' % repr(data)
        if handle in self.openFiles:
            fileObj = self.openFiles[handle]
            d = defer.maybeDeferred(fileObj.close)
            d.addCallback(self._cbClose, handle, requestId)
            d.addErrback(self._ebStatus, requestId, "close failed")
        elif handle in self.openDirs:
            dirObj = self.openDirs[handle][0]
            d = defer.maybeDeferred(dirObj.close)
            d.addCallback(self._cbClose, handle, requestId, 1)
            d.addErrback(self._ebStatus, requestId, "close failed")
        else:
            self._ebClose(failure.Failure(KeyError()), requestId)

    def _cbClose(self, result, handle, requestId, isDir = 0):
        if isDir:
            del self.openDirs[handle]
        else:
            del self.openFiles[handle]
        self._sendStatus(requestId, FX_OK, 'file closed')

    def packet_READ(self, data):
        requestId = data[:4]
        data = data[4:]
        handle, data = getNS(data)
        (offset, length), data = struct.unpack('!QL', data[:12]), data[12:]
        assert data == '', 'still have data in READ: %s' % repr(data)
        if handle not in self.openFiles:
            self._ebRead(failure.Failure(KeyError()), requestId)
        else:
            fileObj = self.openFiles[handle]
            d = defer.maybeDeferred(fileObj.readChunk, offset, length)
            d.addCallback(self._cbRead, requestId)
            d.addErrback(self._ebStatus, requestId, "read failed")

    def _cbRead(self, result, requestId):
        if result == '': # python's read will return this for EOF
            raise EOFError()
        self.sendPacket(FXP_DATA, requestId + NS(result))

    def packet_WRITE(self, data):
        requestId = data[:4]
        data = data[4:]
        handle, data = getNS(data)
        offset, = struct.unpack('!Q', data[:8])
        data = data[8:]
        writeData, data = getNS(data)
        assert data == '', 'still have data in WRITE: %s' % repr(data)
        if handle not in self.openFiles:
            self._ebWrite(failure.Failure(KeyError()), requestId)
        else:
            fileObj = self.openFiles[handle]
            d = defer.maybeDeferred(fileObj.writeChunk, offset, writeData)
            d.addCallback(self._cbStatus, requestId, "write succeeded")
            d.addErrback(self._ebStatus, requestId, "write failed")

    def packet_REMOVE(self, data):
        requestId = data[:4]
        data = data[4:]
        filename, data = getNS(data)
        assert data == '', 'still have data in REMOVE: %s' % repr(data)
        d = defer.maybeDeferred(self.client.removeFile, filename)
        d.addCallback(self._cbStatus, requestId, "remove succeeded")
        d.addErrback(self._ebStatus, requestId, "remove failed")

    def packet_RENAME(self, data):
        requestId = data[:4]
        data = data[4:]
        oldPath, data = getNS(data)
        newPath, data = getNS(data)
        assert data == '', 'still have data in RENAME: %s' % repr(data)
        d = defer.maybeDeferred(self.client.renameFile, oldPath, newPath)
        d.addCallback(self._cbStatus, requestId, "rename succeeded")
        d.addErrback(self._ebStatus, requestId, "rename failed")

    def packet_MKDIR(self, data):
        requestId = data[:4]
        data = data[4:]
        path, data = getNS(data)
        attrs, data = self._parseAttributes(data)
        assert data == '', 'still have data in MKDIR: %s' % repr(data)
        d = defer.maybeDeferred(self.client.makeDirectory, path, attrs)
        d.addCallback(self._cbStatus, requestId, "mkdir succeeded")
        d.addErrback(self._ebStatus, requestId, "mkdir failed")

    def packet_RMDIR(self, data):
        requestId = data[:4]
        data = data[4:]
        path, data = getNS(data)
        assert data == '', 'still have data in RMDIR: %s' % repr(data)
        d = defer.maybeDeferred(self.client.removeDirectory, path)
        d.addCallback(self._cbStatus, requestId, "rmdir succeeded")
        d.addErrback(self._ebStatus, requestId, "rmdir failed")

    def packet_OPENDIR(self, data):
        requestId = data[:4]
        data = data[4:]
        path, data = getNS(data)
        assert data == '', 'still have data in OPENDIR: %s' % repr(data)
        d = defer.maybeDeferred(self.client.openDirectory, path)
        d.addCallback(self._cbOpenDirectory, requestId)
        d.addErrback(self._ebStatus, requestId, "opendir failed")

    def _cbOpenDirectory(self, dirObj, requestId):
        handle = str(hash(dirObj))
        if handle in self.openDirs:
            raise KeyError, "already opened this directory"
        self.openDirs[handle] = [dirObj, iter(dirObj)]
        self.sendPacket(FXP_HANDLE, requestId + NS(handle))

    def packet_READDIR(self, data):
        requestId = data[:4]
        data = data[4:]
        handle, data = getNS(data)
        assert data == '', 'still have data in READDIR: %s' % repr(data)
        if handle not in self.openDirs:
            self._ebStatus(failure.Failure(KeyError()), requestId)
        else:
            dirObj, dirIter = self.openDirs[handle]
            d = defer.maybeDeferred(self._scanDirectory, dirIter, [])
            d.addCallback(self._cbSendDirectory, requestId)
            d.addErrback(self._ebStatus, requestId, "scan directory failed")

    def _scanDirectory(self, dirIter, f):
        while len(f) < 250:
            try:
                info = dirIter.next()
            except StopIteration:
                if not f:
                    raise EOFError
                return f
            if isinstance(info, defer.Deferred):
                info.addCallback(self._cbScanDirectory, dirIter, f)
                return
            else:
                f.append(info)
        return f

    def _cbScanDirectory(self, result, dirIter, f):
        f.append(result)
        return self._scanDirectory(dirIter, f)

    def _cbSendDirectory(self, result, requestId):
        data = ''
        for (filename, longname, attrs) in result:
            data += NS(filename)
            data += NS(longname)
            data += self._packAttributes(attrs)
        self.sendPacket(FXP_NAME, requestId + 
                        struct.pack('!L', len(result))+data)

    def packet_STAT(self, data, followLinks = 1):
        requestId = data[:4]
        data = data[4:]
        path, data = getNS(data)
        assert data == '', 'still have data in STAT/LSTAT: %s' % repr(data)
        d = defer.maybeDeferred(self.client.getAttrs, path, followLinks)
        d.addCallback(self._cbStat, requestId)
        d.addErrback(self._ebStatus, requestId, 'stat/lstat failed')

    def packet_LSTAT(self, data):
        self.packet_STAT(data, 0)

    def packet_FSTAT(self, data):
        requestId = data[:4]
        data = data[4:]
        handle, data = getNS(data)
        assert data == '', 'still have data in FSTAT: %s' % repr(data)
        if handle not in self.openFiles:
            self._ebStatus(failure.Failure(KeyError('%s not in self.openFiles' 
                                        % handle)), requestId)
        else:
            fileObj = self.openFiles[handle]
            d = defer.maybeDeferred(fileObj.getAttrs)
            d.addCallback(self._cbStat, requestId)
            d.addErrback(self._ebStatus, requestId, 'fstat failed')

    def _cbStat(self, result, requestId):
        data = requestId + self._packAttributes(result)
        self.sendPacket(FXP_ATTRS, data)

    def packet_SETSTAT(self, data):
        requestId = data[:4]
        data = data[4:]
        path, data = getNS(data)
        attrs, data = self._parseAttributes(data)
        if data != '':
            log.msg('WARN: still have data in SETSTAT: %s' % repr(data))
        d = defer.maybeDeferred(self.client.setAttrs, path, attrs)
        d.addCallback(self._cbStatus, requestId, 'setstat succeeded')
        d.addErrback(self._ebStatus, requestId, 'setstat failed')

    def packet_FSETSTAT(self, data):
        requestId = data[:4]
        data = data[4:]
        handle, data = getNS(data)
        attrs, data = self._parseAttributes(data)
        assert data == '', 'still have data in FSETSTAT: %s' % repr(data)
        if handle not in self.openFiles:
            self._ebStatus(failure.Failure(KeyError()), requestId)
        else:
            fileObj = self.openFiles[handle]
            d = defer.maybeDeferred(fileObj.setAttrs, attrs)
            d.addCallback(self._cbStatus, requestId, 'fsetstat succeeded')
            d.addErrback(self._ebStatus, requestId, 'fsetstat failed')

    def packet_READLINK(self, data):
        requestId = data[:4]
        data = data[4:]
        path, data = getNS(data)
        assert data == '', 'still have data in READLINK: %s' % repr(data)
        d = defer.maybeDeferred(self.client.readLink, path)
        d.addCallback(self._cbReadLink, requestId)
        d.addErrback(self._ebStatus, requestId, 'readlink failed')

    def _cbReadLink(self, result, requestId):
        self._cbSendDirectory([(result, '', {})], requestId)

    def packet_SYMLINK(self, data):
        requestId = data[:4]
        data = data[4:]
        linkPath, data = getNS(data)
        targetPath, data = getNS(data)
        d = defer.maybeDeferred(self.client.makeLink, linkPath, targetPath)
        d.addCallback(self._cbStatus, requestId, 'symlink succeeded')
        d.addErrback(self._ebStatus, requestId, 'symlink failed')

    def packet_REALPATH(self, data):
        requestId = data[:4]
        data = data[4:]
        path, data = getNS(data)
        assert data == '', 'still have data in REALPATH: %s' % repr(data)
        d = defer.maybeDeferred(self.client.realPath, path)
        d.addCallback(self._cbReadLink, requestId) # same return format
        d.addErrback(self._ebStatus, requestId, 'realpath failed')

    def packet_EXTENDED(self, data):
        requestId = data[:4]
        data = data[4:]
        extName, extData = getNS(data)
        d = defer.maybeDeferred(self.client.extendedRequest, extName, extData)
        d.addCallback(self._cbExtended, requestId)
        d.addErrback(self._ebStatus, requestId, 'extended %s failed' % extName)

    def _cbExtended(self, data, requestId):
        self.sendPacket(FXP_EXTENDED_REPLY, requestId + data)

    def _cbStatus(self, result, requestId, msg = "request succeeded"):
        self._sendStatus(requestId, FX_OK, msg)

    def _ebStatus(self, reason, requestId, msg = "request failed"):
        code = FX_FAILURE
        message = msg
        if reason.type in (IOError, OSError):
            if reason.value.errno == errno.ENOENT: # no such file
                code = FX_NO_SUCH_FILE
                message = reason.value.strerror
            elif reason.value.errno == errno.EACCES: # permission denied
                code = FX_PERMISSION_DENIED
                message = reason.value.strerror
            else:
                log.err(reason)
        elif reason.type == EOFError: # EOF
            code = FX_EOF
            if reason.value.args:
                message = reason.value.args[0]
        elif reason.type == NotImplementedError:
            code = FX_OP_UNSUPPORTED
            if reason.value.args:
                message = reason.value.args[0]
        elif reason.type == SFTPError:
            code = reason.value.code
            message = reason.value.message
        else:
            log.err(reason)
        self._sendStatus(requestId, code, message)

    def _sendStatus(self, requestId, code, message, lang = ''):
        """
        Helper method to send a FXP_STATUS message.
        """
        data = requestId + struct.pack('!L', code)
        data += NS(message)
        data += NS(lang)
        self.sendPacket(FXP_STATUS, data)

class FileTransferClient(FileTransferBase):

    def __init__(self, extData = {}):
        """
        extData is a dict of extended_name : extended_data items
        to be sent to the server.
        """
        FileTransferBase.__init__(self)
        self.extData = {}
        self.counter = 0
        self.openRequests = {} # id -> Deferred
        self.wasAFile = {} # Deferred -> 1 TERRIBLE HACK

    def connectionMade(self):
        data = struct.pack('!L', max(self.versions))
        for k,v in self.extData.itervalues():
            data += NS(k) + NS(v)
        self.sendPacket(FXP_INIT, data)

    def _sendRequest(self, msg, data):
        data = struct.pack('!L', self.counter) + data
        d = defer.Deferred()
        self.openRequests[self.counter] = d
        self.counter += 1
        self.sendPacket(msg, data)
        return d

    def _parseRequest(self, data):
        (id,) = struct.unpack('!L', data[:4])
        d = self.openRequests[id]
        del self.openRequests[id]
        return d, data[4:]

    def openFile(self, filename, flags, attrs):
        """
        Open a file.

        filename is a string representing the file to open.

        flags is a integer of the flags to open the file with, ORed together.
        The flags and their values are listed at the bottom of this file.

        attrs is a list of attributes to open the file with.  It is a
        dictionary, consisting of 0 or more keys.  The possible keys are:
            size: the size of the file in bytes
            uid: the user ID of the file as an integer
            gid: the group ID of the file as an integer
            permissions: the permissions of the file with as an integer.
            the bit representation of this field is defined by POSIX.
            atime: the access time of the file as seconds since the epoch.
            mtime: the modification time of the file as seconds since the epoch.
            ext_*: extended attributes.  The server is not required to
            understand this, but it may.

        NOTE: there is no way to indicate text or binary files.  it is up
        to the SFTP client to deal with this.

        This method returns an Deferred that is called back with an object
        that meets the ISFTPFile interface.
        """
        data = NS(filename) + struct.pack('!L', flags) + self._packAttributes(attrs)
        d = self._sendRequest(FXP_OPEN, data)
        self.wasAFile[d] = (1, filename) # HACK
        return d

    def removeFile(self, filename):
        """
        Remove the given file.

        filename is the name of the file as a string.

        This method returns a Deferred that is called back when it succeeds.
        """
        return self._sendRequest(FXP_REMOVE, NS(filename))

    def renameFile(self, oldpath, newpath):
        """
        Rename the given file.

        oldpath is the current location of the file.
        newpath is the new file name.

        This method returns a Deferred that is called back when it succeeds.
        """
        return self._sendRequest(FXP_RENAME, NS(oldpath)+NS(newpath))

    def makeDirectory(self, path, attrs):
        """
        Make a directory.

        path is the name of the directory to create as a string.
        attrs is a dictionary of attributes to create the directory with.
        It's meaning is the same as the attrs in the openFile method.

        This method returns a Deferred that is called back when it is 
        created.
        """
        return self._sendRequest(FXP_MKDIR, NS(path)+self._packAttributes(attrs))

    def removeDirectory(self, path):
        """
        Remove a directory (non-recursively)

        path is the directory to remove.

        It is an error to remove a directory that has files or directories in
        it.

        This method returns a Deferred that is called back when it is removed.
        """
        return self._sendRequest(FXP_RMDIR, NS(path))

    def openDirectory(self, path):
        """
        Open a directory for scanning.

        path is the directory to open.

        This method returns a Deferred that is called back with an iterable 
        object that has a close() method,

        The close() method is called when the client is finished reading
        from the directory.  At this point, the iterable will no longer
        be used.

        The iterable returns triples of the form (filename, longname, attrs)
        or a Deferred that returns the same.  The sequence must support 
        __getitem__, but otherwise may be any 'sequence-like' object.

        filename is the name of the file relative to the directory.
        logname is an expanded format of the filename.  The recommended format
        is:
        -rwxr-xr-x   1 mjos     staff      348911 Mar 25 14:29 t-filexfer
        1234567890 123 12345678 12345678 12345678 123456789012

        The first line is sample output, the second is the length of the field.
        The fields are: permissions, link count, user owner, group owner,
        size in bytes, modification time.

        attrs is a dictionary in the format of the attrs argument to openFile.
        """
        d = self._sendRequest(FXP_OPENDIR, NS(path))
        self.wasAFile[d] = (0, path)
        return d

    def getAttrs(self, path, followLinks=0):
        """
        Return the attributes for the given path.

        path is the path to return attributes for as a string.
        followLinks is a boolean.  if it is True, follow symbolic links
        and return attributes for the real path at the base.  if it is False,
        return attributes for the specified path.

        This method returns a dictionary in the same format as the attrs
        argument to openFile or a Deferred that is called back with same.
        """
        if followLinks: m = FXP_STAT
        else: m = FXP_LSTAT
        return self._sendRequest(m, NS(path))

    def setAttrs(self, path, attrs):
        """
        Set the attributes for the path.

        path is the path to set attributes for as a string.
        attrs is a idctionary in the same format as the attrs argument to
        openFile.

        This method returns when the attributes are set or a Deferred that is
        called back when they are.
        """
        data = NS(path) + self._packAttributes(attrs)
        return self._sendRequest(FXP_SETSTAT, data)

    def readLink(self, path):
        """
        Find the root of a set of symbolic links.

        path is the path of the symlink to read.

        This method returns the target of the link, or a Deferred that
        returns the same.
        """
        d = self._sendRequest(FXP_READLINK, NS(path))
        return d.addCallback(self._cbRealPath)

    def makeLink(self, linkPath, targetPath):
        """
        Create a symbolic link.

        linkPath is is the pathname of the symlink as a string
        targetPath is the path of the target of the link as a string.

        This method returns when the link is made, or a Deferred that
        returns the same.
        """
        return self._sendRequest(FXP_SYMLINK, NS(linkPath)+NS(targetPath))

    def realPath(self, path):
        """
        Convert any path to an absolute path.

        path is the path to convert as a string.

        This method returns the absolute path as a string, or a Deferred
        that returns the same.
        """
        d = self._sendRequest(FXP_REALPATH, NS(path))
        return d.addCallback(self._cbRealPath)

    def _cbRealPath(self, result):
        name, longname, attrs = result[0]
        return name

    def extendedRequest(self, request, data):
        """
        Make an extended request of the server.
        request is the name of the extended request to make.
        data is any other data that goes along with the request.

        The method returns a Deferred that is called back with
        the result of the extended request.
        """
        return self._sendRequest(FXP_EXTENDED, NS(request) + data)

    def packet_VERSION(self, data):
        version, = struct.unpack('!L', data[:4])
        data = data[4:]
        d = {}
        while data:
            k, data = getNS(data)
            v, data = getNS(data)
            d[k]=v
        self.version = version
        self.gotServerVersion(version, d)

    def packet_STATUS(self, data):
        d, data = self._parseRequest(data)
        code, = struct.unpack('!L', data[:4])
        data = data[4:]
        msg, data = getNS(data)
        lang = getNS(data)
        if code == FX_OK:
            d.callback((msg, lang))
        elif code == FX_EOF:
            d.errback(EOFError(msg))
        elif code == FX_OP_UNSUPPORTED:
            d.errback(NotImplementedError(msg))
        else:
            d.errback(SFTPError(code, msg, lang))

    def packet_HANDLE(self, data):
        d, data = self._parseRequest(data)
        isFile, name = self.wasAFile.pop(d)
        if isFile:
            cb = ClientFile(self, getNS(data)[0])
        else:
            cb = ClientDirectory(self, getNS(data)[0])
        cb.name = name
        d.callback(cb)

    def packet_DATA(self, data):
        d, data = self._parseRequest(data)
        d.callback(getNS(data)[0])

    def packet_NAME(self, data):
        d, data = self._parseRequest(data)
        count, = struct.unpack('!L', data[:4])
        data = data[4:]
        files = []
        for i in range(count):
            filename, data = getNS(data)
            longname, data = getNS(data)
            attrs, data = self._parseAttributes(data)
            files.append((filename, longname, attrs))
        d.callback(files)

    def packet_ATTRS(self, data):
        d, data = self._parseRequest(data)
        d.callback(self._parseAttributes(data)[0])

    def packet_EXTENDED_REPLY(self, data):
        d, data = self._parseRequest(data)
        d.callback(data)

    def gotServerVersion(self, serverVersion, extData):
        """
        Called when the client sends their version info.

        otherVersion is an integer representing the version of the SFTP
        protocol they are claiming.
        extData is a dictionary of extended_name : extended_data items.
        These items are sent by the client to indicate additional features.
        """

class ClientFile:

    interface.implements(ISFTPFile)

    def __init__(self, parent, handle):
        self.parent = parent
        self.handle = NS(handle)

    def close(self):
        return self.parent._sendRequest(FXP_CLOSE, self.handle)

    def readChunk(self, offset, length):
        data = self.handle + struct.pack("!QL", offset, length)
        return self.parent._sendRequest(FXP_READ, data)

    def writeChunk(self, offset, chunk):
        data = self.handle + struct.pack("!Q", offset) + NS(chunk)
        return self.parent._sendRequest(FXP_WRITE, data)

    def getAttrs(self):
        return self.parent._sendRequest(FXP_FSTAT, self.handle)

    def setAttrs(self, attrs):
        data = self.handle + self.parent._packAttributes(attrs)
        return self.parent._sendRequest(FXP_FSTAT, data)

class ClientDirectory:

    def __init__(self, parent, handle):
        self.parent = parent
        self.handle = NS(handle)
        self.filesCache = []

    def read(self):
        d = self.parent._sendRequest(FXP_READDIR, self.handle)
        return d

    def close(self):
        return self.parent._sendRequest(FXP_CLOSE, self.handle)

    def __iter__(self):
        return self

    def next(self):
        if self.filesCache:
            return self.filesCache.pop(0)
        d = self.read()
        d.addCallback(self._cbReadDir)
        d.addErrback(self._ebReadDir)
        return d

    def _cbReadDir(self, names):
        self.filesCache = names[1:]
        return names[0]

    def _ebReadDir(self, reason):
        reason.trap(EOFError)
        def _():
            raise StopIteration
        self.next = _
        return reason


class SFTPError(Exception):

    def __init__(self, errorCode, errorMessage, lang = ''):
        Exception.__init__(self)
        self.code = errorCode
        self.message = errorMessage
        self.lang = lang

    def __str__(self):
        return 'SFTPError %s: %s' % (self.code, self.message)

FXP_INIT            =   1
FXP_VERSION         =   2
FXP_OPEN            =   3
FXP_CLOSE           =   4
FXP_READ            =   5
FXP_WRITE           =   6
FXP_LSTAT           =   7
FXP_FSTAT           =   8
FXP_SETSTAT         =   9
FXP_FSETSTAT        =  10
FXP_OPENDIR         =  11
FXP_READDIR         =  12
FXP_REMOVE          =  13
FXP_MKDIR           =  14
FXP_RMDIR           =  15
FXP_REALPATH        =  16
FXP_STAT            =  17
FXP_RENAME          =  18
FXP_READLINK        =  19
FXP_SYMLINK         =  20
FXP_STATUS          = 101
FXP_HANDLE          = 102
FXP_DATA            = 103
FXP_NAME            = 104
FXP_ATTRS           = 105
FXP_EXTENDED        = 200
FXP_EXTENDED_REPLY  = 201

FILEXFER_ATTR_SIZE        = 0x00000001
FILEXFER_ATTR_OWNERGROUP  = 0x00000002
FILEXFER_ATTR_PERMISSIONS = 0x00000004
FILEXFER_ATTR_ACMODTIME   = 0x00000009
FILEXFER_ATTR_EXTENDED    = 0x+80000000

FILEXFER_TYPE_REGULAR        = 1
FILEXFER_TYPE_DIRECTORY      = 2
FILEXFER_TYPE_SYMLINK        = 3
FILEXFER_TYPE_SPECIAL        = 4
FILEXFER_TYPE_UNKNOWN        = 5

FXF_READ          = 0x00000001
FXF_WRITE         = 0x00000002
FXF_APPEND        = 0x00000004
FXF_CREAT         = 0x00000008
FXF_TRUNC         = 0x00000010
FXF_EXCL          = 0x00000020
FXF_TEXT          = 0x00000040

FX_OK                          = 0
FX_EOF                         = 1
FX_NO_SUCH_FILE                = 2
FX_PERMISSION_DENIED           = 3
FX_FAILURE                     = 4
FX_BAD_MESSAGE                 = 5
FX_NO_CONNECTION               = 6
FX_CONNECTION_LOST             = 7
FX_OP_UNSUPPORTED              = 8

# initialize FileTransferBase.packetTypes:
g = globals()
for name in g.keys():
    if name.startswith('FXP_'):
        value = g[name]
        FileTransferBase.packetTypes[value] = name[4:]
del g, name, value
