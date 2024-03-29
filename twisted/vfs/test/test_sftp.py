import os
import time

from twisted.conch.ssh.filetransfer import FXF_READ, FXF_WRITE, FXF_CREAT
from twisted.conch.ssh.filetransfer import FXF_APPEND, FXF_EXCL
from twisted.conch.ssh.filetransfer import SFTPError
from twisted.conch.ssh.filetransfer import FX_NO_SUCH_FILE
from twisted.trial import unittest
from twisted.internet import defer

from twisted.vfs import ivfs, pathutils
from twisted.vfs.adapters import sftp
from twisted.vfs.backends import inmem, osfs

sftpAttrs = ['size', 'uid', 'gid', 'nlink', 'mtime', 'atime', 'permissions']
sftpAttrs.sort()

class SFTPAdapterTest(unittest.TestCase):

    def rootDir(self):
        return inmem.FakeDirectory()

    def setUp(self):
        root = self.rootDir()

        # Create a subdirectory 'ned'
        self.ned = ned = root.createDirectory('ned')

        # Create a file 'file.txt'
        self.f = f = root.createFile('file.txt')
        f.open(os.O_WRONLY).writeChunk(0, 'wobble\n')
        f.close()

        self.root = root
        self.avatar = sftp.VFSConchUser('radix', root)
        self.sftp = sftp.AdaptFileSystemUserToISFTP(self.avatar)

    def _assertNodes(self, dir, mynodes):
        nodes = [x[0] for x in pathutils.fetch(self.root, dir).children()]
        nodes.sort()
        mynodes.sort()
        return self.assertEquals(nodes, mynodes)

    def test_openFile(self):
        child = self.sftp.openFile('file.txt', 0, None)
        self.failUnless(ivfs.IFileSystemNode.providedBy(child))

    def test_openNewFile(self):
        # Opening a new file should work if FXF_CREAT is passed
        child = self.sftp.openFile('new file.txt', FXF_READ|FXF_CREAT, None)
        self.failUnless(ivfs.IFileSystemNode.providedBy(child))

        # But opening with FXF_READ alone should fail with FX_NO_SUCH_FILE.
        e = self.assertRaises(SFTPError,
           self.sftp.openFile, 'new file 2.txt', FXF_READ, None)
        self.assertEqual(FX_NO_SUCH_FILE, e.code)

        # The FXF_WRITE flag alone can create a file.
        child = self.sftp.openFile('new file 3.txt', FXF_WRITE, None)
        self.failUnless(ivfs.IFileSystemNode.providedBy(child))

        # So, of course FXF_WRITE plus FXF_READ can too.
        child = self.sftp.openFile('new file 4.txt', FXF_WRITE|FXF_READ, None)
        self.failUnless(ivfs.IFileSystemNode.providedBy(child))

        # The FXF_APPEND flag alone can create a file.
        child = self.sftp.openFile('new file 5.txt', FXF_APPEND, None)
        self.failUnless(ivfs.IFileSystemNode.providedBy(child))

    def test_openNewFileExclusive(self):
        # Creating a file should fail if the FXF_EXCL flag is given and the file
        # already exists.
        flags = FXF_WRITE|FXF_CREAT|FXF_EXCL
        self.assertRaises(ivfs.VFSError,
                       self.sftp.openFile, 'file.txt', flags, None)

        # But if the file doesn't exist, then it should work.
        child = self.sftp.openFile('new file.txt', flags, None)
        self.failUnless(ivfs.IFileSystemNode.providedBy(child))

    def test_removeFile(self):
        self.sftp.removeFile('/file.txt')
        self._assertNodes('/', ['.', '..', 'ned'])

    def test_renameFile(self):
        self.sftp.renameFile('/file.txt', '/radixiscool.txt')
        self._assertNodes('/', ['.', '..', 'ned', 'radixiscool.txt'])

    def test_renameFileRelative(self):
        self.sftp.renameFile('file.txt', 'radixiscool.txt')
        self._assertNodes('/', ['.', '..', 'ned', 'radixiscool.txt'])

    def test_renameToDirectory(self):
        self.sftp.renameFile('/file.txt', '/ned')
        self._assertNodes('/', ['.', '..', 'ned'])
        self._assertNodes('/ned', ['.', '..', 'file.txt'])

    def test_renameInDirectory(self):
        self.sftp.renameFile('/file.txt', '/ned')
        self._assertNodes('/', ['.', '..', 'ned'])
        self._assertNodes('/ned', ['.', '..', 'file.txt'])
        self.sftp.renameFile('/ned/file.txt', '/ned/file2.txt')
        self._assertNodes('/ned', ['.', '..', 'file2.txt'])

    def test_makeDirectory(self):
        self.sftp.makeDirectory('/dir', None)
        self._assertNodes('/', ['.', '..', 'file.txt', 'ned', 'dir'])
        self._assertNodes('/dir', ['.', '..'])

    def test_makeSubDirectory(self):
        self.sftp.makeDirectory('/dir', None)
        self.sftp.makeDirectory('/dir/subdir', None)
        self._assertNodes('/', ['.', '..', 'file.txt', 'ned', 'dir'])
        self._assertNodes('/dir', ['.', '..', 'subdir'])
        self._assertNodes('/dir/subdir', ['.', '..'])

    def test_removeDirectory(self):
        self.sftp.makeDirectory('/dir', None)
        self.sftp.removeDirectory('/dir')
        self._assertNodes('/', ['.', '..', 'file.txt', 'ned'])

    def test_openDirectory(self):
        for name, lsline, attrs in self.sftp.openDirectory('/ned'):
            keys = attrs.keys()
            keys.sort()
            self.failUnless(sftpAttrs, keys)

    def test_getAttrs(self):
        attrs = self.sftp.getAttrs('/ned', None).keys()
        attrs.sort()
        self.failUnless(sftpAttrs, attrs)

    def test_setAttrs(self):
        for mtime in [86401, 200000, int(time.time())]:
            try:
                self.sftp.setAttrs('/file.txt', {'mtime': mtime})
            except NotImplementedError:
                raise unittest.SkipTest(
                    "The VFS backend %r doesn't support setAttrs" 
                    % (self.root,))
            else:
                self.assertEqual(
                    mtime, self.sftp.getAttrs('/file.txt', False)['mtime'])

    def test_dirlistWithoutAttrs(self):
        self.ned.getMetadata = self.f.getMetadata = lambda: {}
        for name, lsline, attrs in self.sftp.openDirectory('/'):
            keys = attrs.keys()
            keys.sort()
            self.failUnless(sftpAttrs, keys)


class SFTPAdapterOSFSTest(SFTPAdapterTest):
    def rootDir(self):
        path = self.mktemp()
        os.mkdir(path)
        return osfs.OSDirectory(path)


class DummyDir(inmem.FakeDirectory):
    def createDirectory(self, childName):
        d = defer.Deferred()
        d2 = defer.maybeDeferred(inmem.FakeDirectory.createDirectory, 
                                 self, childName)
        from twisted.internet import reactor
        reactor.callLater(1, d2.chainDeferred, d)
        return d

class SFTPAdapterDeferredTestCase(unittest.TestCase):
    def setUp(self):
        root = DummyDir()
        filesystem = pathutils.FileSystem(root)
        self.filesystem = filesystem

        avatar = sftp.VFSConchUser('radix', root)
        self.sftp = sftp.AdaptFileSystemUserToISFTP(avatar)

    def _assertNodes(self, dir, mynodes):
        nodes = [x[0] for x in self.filesystem.fetch(dir).children()]
        nodes.sort()
        mynodes.sort()
        return self.assertEquals(nodes, mynodes)

    def test_makeDirectoryDeferred(self):
        # Allow Deferreds to be returned from createDirectory
        d = defer.maybeDeferred(self.sftp.makeDirectory, '/dir', None)
        def cb(result):
            self._assertNodes('/', ['.', '..', 'dir'])
        return d.addCallback(cb)

