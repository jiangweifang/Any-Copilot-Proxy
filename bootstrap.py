"""Create a project environment without modifying an existing Python/Conda installation."""
import argparse
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Install only server dependencies and run the CLI installer")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ is required. Activate a suitable Conda environment or install Python first.")
    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    interpreter = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    print("[1/3] Preparing an isolated environment with", sys.executable, flush=True)
    if not interpreter.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    print("[2/3] Installing " + ("server" if args.headless else "desktop") + " dependencies...", flush=True)
    subprocess.run([str(interpreter), "-m", "pip", "install", "-c", str(root / "constraints.txt"),
                    "-e", str(root) + ("" if args.headless else "[desktop]")], check=True)
    print("[3/3] Opening setup. GitHub device authorization must be completed by you.", flush=True)
    command = [str(interpreter), "-m", "copilot_proxy.cli" if args.headless else "copilot_proxy.desktop",
               *(["install"] if args.headless else ["--setup"]), "--root", str(root)]
    subprocess.run(command, cwd=root, check=True)


if __name__ == "__main__":
    main()
