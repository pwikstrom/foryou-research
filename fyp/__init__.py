# development version
from py_compile import compile
from os import listdir
for fn in listdir():
    if fn.endswith(".py"):
        compile(fn)


from .fyp_main import *
from .machine_annotation import *
from .download_videos import *
from .get_baseline_log import *
from .recode_variables import *
from .donations import *
from .stats import *
from .pca import *


