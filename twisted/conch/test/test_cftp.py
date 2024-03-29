# -*- test-case-name: twisted.conch.test.test_cftp -*-
# Copyright (c) 2001-2004 Twisted Matrix Laboratories.
# See LICENSE file for details.

import sys

from twisted.trial import unittest, util
try:
    from twisted.conch import unix
    from twisted.conch.scripts import cftp
    from twisted.conch.client import connect, default, options
except ImportError:
    unix = None
    try:
        del sys.modules['twisted.conch.unix'] # remove the bad import
    except KeyError:
        # In Python 2.4, the bad import has already been cleaned up for us.
        pass

try:
    import Crypto
except ImportError:
    Crypto = None

from twisted.cred import portal
from twisted.internet import reactor, protocol, interfaces, defer, error
from twisted.internet.utils import getProcessOutputAndValue
from twisted.python import log, failure
from twisted.test import test_process

import test_ssh, test_conch
from test_filetransfer import SFTPTestBase, FileTransferTestAvatar
import sys, os, os.path, time, tempfile

class FileTransferTestRealm:

    def requestAvatar(sefl, avatarID, mind, *interfaces):
        a = FileTransferTestAvatar()
        return interfaces[0], a, lambda: None


class SFTPTestProcess(protocol.ProcessProtocol):

    def __init__(self):
        self.clearBuffer()
        self.connected = 0

    def connectionMade(self):
        self.connected = 1

    def clearBuffer(self):
        self.buffer = ''

    def outReceived(self, data):
        log.msg('got %s' % data)
        self.buffer += data

    def errReceived(self, data):
        log.msg('err: %s' % data)

    def connectionLost(self, reason):
        self.connected = 0
    def getBuffer(self):
        return self.buffer

class CFTPClientTestBase(SFTPTestBase):

    def setUpClass(self):
        open('dsa_test.pub','w').write(test_ssh.publicDSA_openssh)
        open('dsa_test','w').write(test_ssh.privateDSA_openssh)
        os.chmod('dsa_test', 33152)
        open('kh_test','w').write('127.0.0.1 '+test_ssh.publicRSA_openssh)

    def startServer(self):
        realm = FileTransferTestRealm()
        p = portal.Portal(realm)
        p.registerChecker(test_ssh.ConchTestPublicKeyChecker())
        fac = test_ssh.ConchTestServerFactory()
        fac.portal = p
        self.server = reactor.listenTCP(0, fac, interface="127.0.0.1")

    def stopServer(self):
        if hasattr(self.server.factory, 'proto'):
            self.server.factory.proto.expectedLoseConnection = 1
            self.server.factory.proto.transport.loseConnection()
            util.spinWhile(lambda:self.server.factory.proto.transport.connected)
        self.server.stopListening()
        util.spinWhile(lambda:self.server.connected)
        reactor.iterate()

    def tearDownClass(self):
        for f in ['dsa_test.pub', 'dsa_test', 'kh_test']:
            try:
                os.remove(f)
            except:
                pass

class TestOurServerCmdLineClient(test_process.SignalMixin, CFTPClientTestBase):

    def setUpClass(self):
        if hasattr(self, 'skip'):
           return
        test_process.SignalMixin.setUpClass(self)
        CFTPClientTestBase.setUpClass(self)

        self.startServer()
        cmds = ('-p %i -l testuser '
               '--known-hosts kh_test '
               '--user-authentications publickey '
               '--host-key-algorithms ssh-rsa '
               '-K direct '
               '-i dsa_test '
               '-a --nocache '
               '-v '
               '127.0.0.1')
        port = self.server.getHost().port
        cmds = test_conch._makeArgs((cmds % port).split(), mod='cftp')
        log.msg('running %s %s' % (sys.executable, cmds))
        self.processProtocol = SFTPTestProcess()

        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join(sys.path)
        reactor.spawnProcess(self.processProtocol, sys.executable, cmds,
                             env=env)

        timeout = time.time() + 10
        while (not self.processProtocol.buffer) and (time.time() < timeout):
            reactor.iterate(0.1)
        if time.time() > timeout:
            self.skip = "couldn't start process"
        else:
            self.processProtocol.clearBuffer()

    def tearDownClass(self):
        if hasattr(self, 'skip'):
            return
        test_process.SignalMixin.tearDownClass(self)
        CFTPClientTestBase.tearDownClass(self)
        self.stopServer()
        try:
            self.processProtocol.transport.signalProcess('KILL')
        except error.ProcessExitedAlready:
            pass
        reactor.iterate(0.1)
        reactor.iterate(0.1)
        reactor.iterate(0.1)

    def _getCmdResult(self, cmd):
        self.processProtocol.clearBuffer()
        self.processProtocol.transport.write(cmd+'\n')
        timeout = time.time() + 10
        while (self.processProtocol.buffer.find('cftp> ') == -1) and (time.time() < timeout):
            reactor.iterate(0.1)
        self.failIf(time.time() > timeout, "timeout")
        if self.processProtocol.buffer.startswith('cftp> '):
            self.processProtocol.buffer = self.processProtocol.buffer[6:]
        return self.processProtocol.buffer[:-6].strip()

    def testCdPwd(self):
        homeDir = os.path.join(os.getcwd(), 'sftp_test')
        pwdRes = self._getCmdResult('pwd')
        lpwdRes = self._getCmdResult('lpwd')
        cdRes = self._getCmdResult('cd testDirectory')
        self._getCmdResult('cd ..')
        pwd2Res = self._getCmdResult('pwd')
        self.failUnlessEqual(pwdRes, homeDir)
        self.failUnlessEqual(lpwdRes, os.getcwd())
        self.failUnlessEqual(cdRes, '')
        self.failUnlessEqual(pwd2Res, pwdRes)

    def testChAttrs(self):
       lsRes = self._getCmdResult('ls -l testfile1')
       self.failUnless(lsRes.startswith('-rw-r--r--'), lsRes)
       self.failIf(self._getCmdResult('chmod 0 testfile1'))
       lsRes = self._getCmdResult('ls -l testfile1')
       self.failUnless(lsRes.startswith('----------'), lsRes)
       self.failIf(self._getCmdResult('chmod 644 testfile1'))
       log.flushErrors()
       # XXX test chgrp/own

    def testList(self):
        lsRes = self._getCmdResult('ls').split('\n')
        self.failUnlessEqual(lsRes, ['testDirectory', 'testRemoveFile', \
                'testRenameFile', 'testfile1'])
        lsRes = self._getCmdResult('ls ../sftp_test').split('\n')
        self.failUnlessEqual(lsRes, ['testDirectory', 'testRemoveFile', \
                'testRenameFile', 'testfile1'])
        lsRes = self._getCmdResult('ls *File').split('\n')
        self.failUnlessEqual(lsRes, ['testRemoveFile', 'testRenameFile'])
        lsRes = self._getCmdResult('ls -a *File').split('\n')
        self.failUnlessEqual(lsRes, ['.testHiddenFile', 'testRemoveFile', 'testRenameFile'])
        lsRes = self._getCmdResult('ls -l testDirectory')
        self.failIf(lsRes)
        # XXX test lls in a way that doesn't depend on local semantics

    def testHelp(self):
        helpRes = self._getCmdResult('?')
        self.failUnlessEqual(helpRes, cftp.StdioClient(None).cmd_HELP('').strip())

    def _failUnlessFilesEqual(self, name1, name2, msg=None):
        f1 = file(name1).read()
        f2 = file(name2).read()
        self.failUnlessEqual(f1, f2, msg)

    def testGet(self):
        getRes = self._getCmdResult('get testfile1 "sftp_test/test file2"')
        self._failUnlessFilesEqual('sftp_test/testfile1',
                'sftp_test/test file2', "get failed")
        self.failUnless(getRes.endswith("Transferred %s/sftp_test/testfile1 to sftp_test/test file2" % os.getcwd()))
        self.failIf(self._getCmdResult('rm "test file2"'))
        self.failIf(os.path.exists('sftp_test/test file2'))

    def testWildcardGet(self):
        getRes = self._getCmdResult('get testR*')
        self._failUnlessFilesEqual('sftp_test/testRemoveFile',
                'testRemoveFile', 'testRemoveFile get failed')
        self._failUnlessFilesEqual('sftp_test/testRenameFile',
                'testRenameFile', 'testRenameFile get failed')
        os.remove('testRemoveFile')
        os.remove('testRenameFile')

    def testPut(self):
        putRes = self._getCmdResult('put sftp_test/testfile1 "test\\"file2"')
        f1 = file('sftp_test/testfile1').read()
        f2 = file('sftp_test/test"file2').read()
        self.failUnlessEqual(f1, f2, "put failed")
        self.failUnless(putRes.endswith('Transferred sftp_test/testfile1 to %s/sftp_test/test"file2' % os.getcwd()))
        self.failIf(self._getCmdResult('rm "test\\"file2"'))
        self.failIf(os.path.exists('sftp_test/test"file2'))

    def testWildcardPut(self):
        self.failIf(self._getCmdResult('cd ..'))
        getRes = self._getCmdResult('put sftp_test/testR*')
        self._failUnlessFilesEqual('sftp_test/testRemoveFile',
                'testRemoveFile', 'testRemoveFile get failed')
        self._failUnlessFilesEqual('sftp_test/testRenameFile',
                'testRenameFile', 'testRenameFile get failed')
        self.failIf(self._getCmdResult('cd sftp_test'))
        os.remove('testRemoveFile')
        os.remove('testRenameFile')

    def testLink(self):
        linkRes = self._getCmdResult('ln testLink testfile1')
        self.failIf(linkRes)
        lslRes = self._getCmdResult('ls -l testLink')
        log.flushErrors()
        self.failUnless(lslRes.startswith('l'), 'link failed')
        self.failIf(self._getCmdResult('rm testLink'))

    def testDirectory(self):
        self.failIf(self._getCmdResult('mkdir testMakeDirectory'))
        lslRes = self._getCmdResult('ls -l testMakeDirector?')
        self.failUnless(lslRes.startswith('d'), lslRes)
        self.failIf(self._getCmdResult('rmdir testMakeDirectory'))
        self.failIf(self._getCmdResult('lmkdir sftp_test/testLocalDirectory'))
        self.failIf(self._getCmdResult('rmdir testLocalDirectory'))

    def testRename(self):
        self.failIf(self._getCmdResult('rename testfile1 testfile2'))
        lsRes = self._getCmdResult('ls testfile?').split('\n')
        self.failUnlessEqual(lsRes, ['testfile2'])
        self.failIf(self._getCmdResult('rename testfile2 testfile1'))

    def testCommand(self):
        cmdRes = self._getCmdResult('!echo hello')
        self.failUnlessEqual(cmdRes, 'hello')

class TestOurServerBatchFile(test_process.SignalMixin, CFTPClientTestBase):

    def setUpClass(self):
        test_process.SignalMixin.setUpClass(self)
        CFTPClientTestBase.setUpClass(self)

    def setUp(self):
        CFTPClientTestBase.setUp(self)
        self.startServer()

    def tearDown(self):
        CFTPClientTestBase.tearDown(self)
        self.stopServer()

    def tearDownClass(self):
        test_process.SignalMixin.tearDownClass(self)
        CFTPClientTestBase.tearDownClass(self)

    def _getBatchOutput(self, f):
        fn = tempfile.mktemp()
        open(fn, 'w').write(f)
        l = []
        port = self.server.getHost().port
        cmds = ('-p %i -l testuser '
                    '--known-hosts kh_test '
                    '--user-authentications publickey '
                    '--host-key-algorithms ssh-rsa '
                    '-K direct '
                    '-i dsa_test '
                    '-a --nocache '
                    '-v -b %s 127.0.0.1') % (port, fn)
        cmds = test_conch._makeArgs(cmds.split(), mod='cftp')[1:]
        log.msg('running %s %s' % (sys.executable, cmds))
        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join(sys.path)
        d = getProcessOutputAndValue(sys.executable, cmds, env=env)
        d.setTimeout(10)
        d.addBoth(l.append)
        while not l:
            if hasattr(self.server.factory, 'proto'):
                self.server.factory.proto.expectedLoseConnection = 1
            reactor.iterate(0.1)
        os.remove(fn)
        result = l[0]
        if isinstance(result, failure.Failure):
            raise result.value
        else:
            log.msg(result[1])
            return result[0]

    def testBatchFile(self):
        cmds = """pwd
ls
exit
"""
        res = self._getBatchOutput(cmds).split('\n')
        log.msg('RES %s' % str(res))
        self.failUnless(res[1].find('sftp_test') != -1)
        self.failUnlessEqual(res[3:-2], ['testDirectory', 'testRemoveFile', 'testRenameFile', 'testfile1'])

    def testError(self):
        cmds = """chown 0 missingFile
pwd
exit
"""
        res = self._getBatchOutput(cmds)
        self.failIf(res.find('sftp_test') != -1)

    def testIgnoredError(self):
        cmds = """-chown 0 missingFile
pwd
exit
"""
        res = self._getBatchOutput(cmds)
        self.failIf(res.find('sftp_test') == -1)

class TestOurServerUnixClient(test_process.SignalMixin, CFTPClientTestBase):

    def setUpClass(self):
        if hasattr(self, 'skip'):
            return
        test_process.SignalMixin.setUpClass(self)
        CFTPClientTestBase.setUpClass(self)
        self.startServer()
        cmd1 = ('-p %i -l testuser '
                '--known-hosts kh_test '
                '--host-key-algorithms ssh-rsa '
                '-a '
                '-K direct '
                '-i dsa_test '
                '127.0.0.1'
                )
        port = self.server.getHost().port
        cmds1 = (cmd1 % port).split()
        o = options.ConchOptions()
        def _(host, *args):
            o['host'] = host
        o.parseArgs = _
        o.parseOptions(cmds1)
        vhk = default.verifyHostKey
        self.conn = conn = test_conch.SSHTestConnectionForUnix(None)
        uao = default.SSHUserAuthClient(o['user'], o, conn)
        connect.connect(o['host'], int(o['port']), o, vhk, uao)
        util.spinWhile(lambda: not conn.connected)

    def tearDownClass(self):
        test_process.SignalMixin.tearDownClass(self)
        CFTPClientTestBase.tearDownClass(self)
        self.stopServer()

    def _getBatchOutput(self, f):
        fn = tempfile.mktemp()
        open(fn, 'w').write(f)
        l = []
        port = self.server.getHost().port
        cmds = ('-p %i -l testuser '
                    '-K unix '
                    '-a '
                    '-v -b %s 127.0.0.1') % (port, fn)
        cmds = test_conch._makeArgs(cmds.split(), mod='cftp')[1:]
        log.msg('running %s %s' % (sys.executable, cmds))
        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join(sys.path)
        d = getProcessOutputAndValue(sys.executable, cmds, env=env)
        d.setTimeout(10)
        d.addBoth(l.append)
        while not l:
            if hasattr(self.server.factory, 'proto'):
                self.server.factory.proto.expectedLoseConnection = 1
            reactor.iterate(0.1)
        os.remove(fn)
        result = l[0]
        if isinstance(result, failure.Failure):
            raise result.value
        else:
            log.msg(result[1])
            return result[0]

    def testBatchFile(self):
        cmds = """pwd
exit
"""
        res = self._getBatchOutput(cmds)
        self.failIf(res.find('sftp_test') == -1, "sftp_test not in %r" % (res,))
        self.conn.transport.loseConnection()
        util.spinWhile(lambda:not self.conn.transport.transport.connected)
        self.stopServer()

if not unix or not Crypto or not interfaces.IReactorProcess(reactor, None):
    TestOurServerCmdLineClient.skip = "don't run w/o spawnprocess or PyCrypto"
    TestOurServerBatchFile.skip = "don't run w/o spawnProcess or PyCrypto"
    TestOurServerUnixClient.skip = "don't run w/o spawnProcess or PyCrypto"
