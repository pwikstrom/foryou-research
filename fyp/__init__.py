# development version
from py_compile import compile
from os import listdir
for fn in listdir():
    if fn.endswith(".py"):
        compile(fn)


from .fyp_main import *
from .data_io import *
from .machine_annotation import *
from .scrape import *
from .zeeschuimer import *
from .recode_variables import *
from .donations import *
from .stats import *
from .pca import *
from .organize_datasets import *


