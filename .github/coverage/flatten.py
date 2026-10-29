import sys

from coverage import CoverageData

source = CoverageData(basename=sys.argv[1])
source.read()
flat = CoverageData(basename=sys.argv[2])
files = source.measured_files()
flat.add_arcs({path: source.arcs(path) or [] for path in files})
flat.touch_files(files)
flat.add_file_tracers({path: tracer for path in files if (tracer := source.file_tracer(path))})
flat.write()
