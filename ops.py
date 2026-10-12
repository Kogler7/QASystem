"""Host-side launcher for repository and packaged deployments."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
source = root if (root / 'docker' / 'ops.py').is_file() else root / 'runtime'
sys.path.insert(0, str(source))

from docker.ops import main

if __name__ == '__main__':
    # Resolve the deployment root independently of the caller's working directory.
    raise SystemExit(main(['--project-dir', str(root), *sys.argv[1:]]))
