import os
import pickle
import subprocess
import sys

subprocess.call(sys.argv[1], shell=True)
os.system(sys.argv[1])
pickle.loads(sys.stdin.buffer.read())
