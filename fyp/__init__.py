# development version
import os
from py_compile import compile

package_dir = os.path.dirname(__file__)
for fn in os.listdir(package_dir):
    if fn.endswith(".py"):
        try:
            compile(os.path.join(package_dir, fn))
        except:
            pass
