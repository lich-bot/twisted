# -*- test-case-name: twisted.vfs.test.test_sftp -*-

import os, time

import zope.interface

from twisted.python import components, log
from twisted.conch.avatar import ConchUser
from twisted.conch.interfaces import ISession
from twisted.conch.ssh.filetransfer import ISFTPServer, FileTransferServer
from twisted.conch.ssh.filetransfer import FXF_READ, FXF_WRITE, FXF_APPEND, FXF_CREAT, FXF_TRUNC, FXF_EXCL
from twisted.conch.ssh.filetransfer import SFTPError
from twisted.conch.ssh.filetransfer import FX_PERMISSION_DENIED, FX_FAILURE
from twisted.conch.ssh.filetransfer import FX_NO_SUCH_FILE
from twisted.conch.ssh import session
from twisted.conch.ls import lsLine

from twisted.vfs import ivfs, pathutils


class AdaptFileSystemUserToISFTP:

    zope.interface.implements( ISFTPServer )

    def __init__(self, avatar):
        self.avatar = avatar
        self.openFiles = {}
        self.openDirs = {}
        self.filesystem = avatar.filesystem


    def _setAttrs(self, path, attrs):
        """
        NOTE: this function assumes it runs as the logged-in user:
        i.e. under _runAsUser()
        """
        if attrs.has_key("uid") and attrs.has_key("gid"):
            os.lchown(path, attrs["uid"], attrs["gid"])
        if attrs.has_key("permissions"):
            os.chmod(path, attrs["permissions"])
        if attrs.has_key("atime") and attrs.has_key("mtime"):
            os.utime(path, (attrs["atime"]. attrs["mtime"]))

    def gotVersion(self, otherVersion, extData):
        return {}

    def openFile(self, filename, flags, attrs):
        createPlease = False
        exclusive = False
        openFlags = 0
        if flags & FXF_READ == FXF_READ and flags & FXF_WRITE == 0:
            openFlags = os.O_RDONLY
        if flags & FXF_WRITE == FXF_WRITE and flags & FXF_READ == 0:
            createPlease = True
            openFlags = os.O_WRONLY
        if flags & FXF_WRITE == FXF_WRITE and flags & FXF_READ == FXF_READ:
            createPlease = True
            openFlags = os.O_RDWR
        if flags & FXF_APPEND == FXF_APPEND:
            createPlease = True
            openFlags |= os.O_APPEND
        if flags & FXF_CREAT == FXF_CREAT:
            createPlease = True
            openFlags |= os.O_CREAT
        if flags & FXF_TRUNC == FXF_TRUNC:
            openFlags |= os.O_TRUNC
        if flags & FXF_EXCL == FXF_EXCL:
            exclusive = True

        # XXX Once we change readChunk/writeChunk we'll have to wrap
        # child in something that implements those.

        pathSegments = self.filesystem.splitPath(filename)
        dirname, basename = pathSegments[:-1], pathSegments[-1]
        try:
            parentNode = self.filesystem.fetch('/'.join(dirname))
        except ivfs.NotFoundError, e:
            raise SFTPError(FX_NO_SUCH_FILE, e.args[0])
        except KeyError, e: # XXX: awful exception to catch
            raise SFTPError(FX_NO_SUCH_FILE, e.args[0])
        if createPlease:
            child = parentNode.createFile(basename, exclusive)
        elif parentNode.exists(basename):
            child = parentNode.child(basename)
        else:
            raise SFTPError(FX_NO_SUCH_FILE, filename)
        child.open(openFlags)
        return child

    def removeFile(self, filename):
        self.filesystem.fetch(filename).remove()

    def renameFile(self, oldpath, newpath):
        try:
            targetNode = self.filesystem.fetch(newpath)
        except:
            # XXX: bare excepts are evil!
            pass
        else:
            if ivfs.IFileSystemContainer(targetNode, None):
                oldNode = self.filesystem.fetch(oldpath)
                newpath = self.filesystem.joinPath(
                    newpath, self.filesystem.basename(oldpath))
        try:
            old = self.filesystem.fetch(oldpath)
        except ivfs.NotFoundError, e:
            raise SFTPError(FX_NO_SUCH_FILE, e.args[0])
        else:
            old.rename(newpath)

    def makeDirectory(self, path, attrs):
        dirname  = self.filesystem.dirname(path)
        basename = self.filesystem.basename(path)
        try:
            return self.filesystem.fetch(dirname).createDirectory(basename)
        except ivfs.PermissionError, e:
            raise SFTPError(FX_PERMISSION_DENIED, str(e))
        except ivfs.NotFoundError, e:
            raise SFTPError(FX_NO_SUCH_FILE, e.args[0])
        except ivfs.VFSError, e:
            raise SFTPError(FX_FAILURE, str(e))

    def removeDirectory(self, path):
        self.filesystem.fetch(path).remove()

    def openDirectory(self, path):
        class DirList:
            def __init__(self, iter):
                self.iter = iter
            def __iter__(self):
                return self

            def next(self):

                (name, attrs) = self.iter.next()

                class st:
                    pass

                s = st()
                s.st_mode   = attrs["permissions"]
                s.st_uid    = attrs["uid"]
                s.st_gid    = attrs["gid"]
                s.st_size   = attrs["size"]
                s.st_mtime  = attrs["mtime"]
                s.st_nlink  = attrs["nlink"]
                return ( name, lsLine(name, s), attrs )

            def close(self):
                return

        return DirList(
            iter([(name, self._attrify(file))
                  for (name, file) in self.filesystem.fetch(path).children()]))

    def _attrify(self, node):
        meta = node.getMetadata()
        permissions = meta.get('permissions', None)
        if permissions is None:
            if ivfs.IFileSystemContainer.providedBy(node):
                permissions = 16877
            else:
                permissions = 33188

        return {'permissions': permissions,
                'size': meta.get('size', 0),
                'uid': meta.get('uid', 0),
                'gid': meta.get('gid', 0),
                'atime': meta.get('atime', time.time()),
                'mtime': meta.get('mtime', time.time()),
                'nlink': meta.get('nlink', 1)
                }

    def getAttrs(self, path, followLinks):
        try:
            node = self.filesystem.fetch(path)
        except ivfs.NotFoundError, e:
            raise SFTPError(FX_NO_SUCH_FILE, e.args[0])
        return self._attrify(node)

    def setAttrs(self, path, attrs):
        try:
            node = self.filesystem.fetch(path)
        except ivfs.NotFoundError, e:
            raise SFTPError(FX_NO_SUCH_FILE, e.args[0])
        try:
            # XXX: setMetadata isn't yet part of the IFileSystemNode interface
            # (but it should be).  So we catch AttributeError, and translate it
            # to NotImplementedError because it's slightly nicer for clients.
            node.setMetadata(attrs)
        except AttributeError:
            raise NotImplementedError("NO SETATTR")

    def readLink(self, path):
        raise NotImplementedError("NO LINK")

    def makeLink(self, linkPath, targetPath):
        raise NotImplementedError("NO LINK")

    def realPath(self, path):
        return self.filesystem.absPath(path)



class VFSConchSession:
    zope.interface.implements(ISession)
    def __init__(self, avatar):
        self.avatar = avatar
    def openShell(self, proto):
        self.avatar.conn.transport.transport.loseConnection()
    def getPty(self, term, windowSize, modes):
        pass
    def closed(self):
        log.msg('shell closed')


class VFSConchUser(ConchUser):
    def __init__(self, username, root):
        ConchUser.__init__(self)
        self.username = username
        self.filesystem = pathutils.FileSystem(root)

        self.listeners = {}  # dict mapping (interface, port) -> listener
        self.channelLookup.update(
                {"session": session.SSHSession})
        self.subsystemLookup.update(
                {"sftp": FileTransferServer})

    def logout(self):
        # XXX - this may be broken
        log.msg('avatar %s logging out (%i)' % (self.username, len(self.listeners)))




components.registerAdapter(AdaptFileSystemUserToISFTP, VFSConchUser, ISFTPServer)
components.registerAdapter(VFSConchSession, VFSConchUser, ISession)

