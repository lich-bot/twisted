
import vfsapp

from twisted.vfs.backends import osfs

chrootDir = '.'
root = osfs.OSDirectory(realPath=chrootDir)

application = vfsapp.createVFSApplication(root)

