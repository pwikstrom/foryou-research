# development version
from py_compile import compile
import os
package_dir = os.path.dirname(__file__)
for fn in os.listdir(package_dir):
    if fn.endswith(".py"):
        try:
            compile(os.path.join(package_dir, fn))
        except:
            pass


#from .fyp_main import *
#from .data_io import *
#from .machine_annotation import *
#from .scrape import *
#from .zeeschuimer import *
#from .recode_variables import *
#from .donations import *
#from .stats import *
#from .pca import *
#from .organize_datasets import *
#from .calc_collection_stats import *


