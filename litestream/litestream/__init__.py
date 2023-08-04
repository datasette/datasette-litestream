import os

def bin_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "bin", "litestream"))
