"""Point frozen Pocket Ledger builds at the bundled Tcl/Tk runtime files."""

import os
import sys


base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
tcl_dir = os.path.join(base_dir, "tcl")

os.environ.setdefault("TCL_LIBRARY", os.path.join(tcl_dir, "tcl8.6"))
os.environ.setdefault("TK_LIBRARY", os.path.join(tcl_dir, "tk8.6"))
