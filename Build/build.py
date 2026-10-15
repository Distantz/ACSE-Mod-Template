#!/usr/bin/env python3

import sys
import subprocess
import argparse
import hashlib
import ui_package as pkg
from pathlib import Path
import multiprocessing
from multiprocessing.pool import Pool

# Hash contents first to avoid needless rebuilds

# File types that are outputs of this build script rather than source
# content. They should be excluded when hashing an OVL folder, otherwise the
# hash would change after every build even when nothing actually changed.
HASH_EXCLUDED_EXTENSIONS = {".ovl", ".ppuipkg"}

# Where per-entry content hashes are cached
CACHE_DIRNAME = ".build_cache"

def compute_folder_hash(folder: Path) -> str:
    """
    Compute a deterministic content hash for everything inside `folder`.

    Hashes relative path, file size, and file content for every file found,
    in sorted order, so the result only depends on the actual file tree
    contents.
    """
    hasher = hashlib.sha256()

    if not folder.exists():
        return ""

    files = sorted(
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() not in HASH_EXCLUDED_EXTENSIONS
    )

    for f in files:
        rel = f.relative_to(folder).as_posix()
        hasher.update(rel.encode("utf-8"))
        hasher.update(str(f.stat().st_size).encode("utf-8"))
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                hasher.update(chunk)

    return hasher.hexdigest()

def _cache_file_for(manifest_dir: Path, line: str) -> Path:
    """Path to the cached hash file for a given .ovlpaths entry."""
    cache_dir = manifest_dir / CACHE_DIRNAME
    cache_dir.mkdir(exist_ok=True)
    safe_name = line.lstrip("./").replace("\\", "_").replace("/", "_")
    return cache_dir / f"{safe_name}.hash"

def _read_cached_hash(cache_file: Path) -> str:
    if cache_file.exists():
        try:
            return cache_file.read_text().strip()
        except OSError:
            return ""
    return ""

def _write_cached_hash(cache_file: Path, value: str) -> None:
    cache_file.write_text(value)

def process_uipackages(manifest_dir : Path, ovl_path_line):
    """
    Check for .uipackage file in the OVL directory and process UI packages.
    
    Args:
        manifest_dir: Path to the directory containing Manifest.xml
        ovl_path_line: The path line from .ovlpaths (e.g., "./Main/Test")
    
    Returns:
        List of tuples containing (basis_path, uipackage_folder_path)
    """
    ui_packages = []

    # Mod dir
    ovldata_folder : Path = manifest_dir.parent
    
    # Construct the OVL directory path
    ovl_dir : Path = manifest_dir / ovl_path_line.lstrip('./')
    
    # Check for .uipackage file
    uipackages_list_file = ovl_dir / ".uipackages"
    
    if not uipackages_list_file.exists():
        return ui_packages
    
    print(f"  Found .uipackages file")
    
    # Read the .uipackages file
    with open(uipackages_list_file, 'r') as f:
        lines = f.readlines()
    
    print(f"  Building UI packages...")
    for line in lines:
        line = line.strip()
        
        # Skip empty lines and comments
        if not line or line.startswith('#'):
            continue
        
        # Construct the UI package folder path
        uipackage_folder = ovl_dir / line.lstrip('./')
        print(f"     Building UI package: {line}")
        
        
        if not uipackage_folder.exists():
            print(f"     UI Package: {uipackage_folder} does not exist. Skipping...")
            continue

        uipackage_output = uipackage_folder.parent / f"{uipackage_folder.name}.ppuipkg"
        basic_path = Path.relative_to(ovl_dir, ovldata_folder)
        
        with pkg.PPUIPkgFile(str(basic_path).replace("\\", "/"), str(uipackage_output)) as pkgfile:
            pkgfile.importall(str(uipackage_folder))
            pass
    
    print(f"  Finished building UI packages...")

def _process_single_ovlpath(args):
    """
    Worker function for multiprocessing. Processes one line from .ovlpaths.
    Returns (success: bool, output: str) to keep printed output atomic.
    """
    ovl_tool_cmd, manifest_dir, line_num, line = args

    out = []
    out.append(f"\n[{line_num}] Processing: {line}")

    input_path  = manifest_dir / line.lstrip('./')
    output_path = manifest_dir / f"{line.lstrip('./')}.ovl"

    out.append(f"  Input:  {input_path}")
    out.append(f"  Output: {output_path}")

    process_uipackages(manifest_dir, line)
    out.append("  Packaging OVL...")

    cmd = [
        "python",
        str(ovl_tool_cmd),
        "new",
        "-i", str(input_path),
        "-g", "Planet Coaster 2",
        "-o", str(output_path),
        "--force",
        "--update-aux",  # needed for textures in pc2
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(manifest_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        out.append("  Finished.")
        if result.stdout:
            out.append(f"  Output: {result.stdout.strip()}")
        return True, "\n".join(out)

    except subprocess.CalledProcessError as e:
        out.append(f"  Failed with exit code {e.returncode}")
        if e.stdout:
            out.append(f"  stdout: {e.stdout.strip()}")
        if e.stderr:
            out.append(f"  stderr: {e.stderr.strip()}")
        return False, "\n".join(out)

    except Exception as e:
        out.append(f"  Error: {e}")
        return False, "\n".join(out)


def process_ovlpaths(cobra_tools_path, manifest_path, num_workers=None):
    """
    Process the .ovlpaths file relative to the Manifest.xml location
    and run ovl_tool_cmd.py for each path entry in parallel.

    Args:
        cobra_tools_path: Path to cobra tools
        manifest_path:    Path to the Manifest.xml file
        num_workers:      Number of parallel workers (default: cpu_count)
    """
    manifest_dir = Path(manifest_path).parent.resolve()

    if not cobra_tools_path:
        print("Error: COBRA_TOOLS_PATH environment variable is not set", file=sys.stderr)
        return False

    cobra_tools_path = Path(cobra_tools_path)
    ovl_tool_cmd     = cobra_tools_path / "ovl_tool_cmd.py"

    if not ovl_tool_cmd.exists():
        print(f"Error: ovl_tool_cmd.py not found at {ovl_tool_cmd}", file=sys.stderr)
        return False

    ovlpaths_file = manifest_dir / ".ovlpaths"

    if not ovlpaths_file.exists():
        print(f"Error: .ovlpaths file not found at {ovlpaths_file}", file=sys.stderr)
        return False

    print(f"Processing .ovlpaths from: {ovlpaths_file}")
    print(f"Manifest directory:        {manifest_dir}")
    print(f"Cobra tools path:          {cobra_tools_path}")
    print("-" * 60)

    with open(ovlpaths_file, 'r') as f:
        lines = f.readlines()

    # Build the work list, skipping blanks / comments
    tasks = [
        (ovl_tool_cmd, manifest_dir, line_num, line.strip())
        for line_num, line in enumerate(lines, 1)
        if line.strip() and not line.strip().startswith('#')
    ]

    if not tasks:
        print("No entries found in .ovlpaths.")
        return True

    workers = num_workers or multiprocessing.cpu_count()
    print(f"Spawning {min(workers, len(tasks))} worker(s) for {len(tasks)} task(s).\n")

    success_count = 0
    fail_count    = 0

    with Pool(processes=workers) as pool:
        for success, output in pool.imap_unordered(_process_single_ovlpath, tasks):
            print(output)
            if success:
                success_count += 1
            else:
                fail_count += 1

    print("\n" + "=" * 60)
    print(f"Processing complete: {success_count} succeeded, {fail_count} failed")

    return fail_count == 0


def main():
    parser = argparse.ArgumentParser(
        description="Process .ovlpaths file and run ovl_tool_cmd.py for each entry"
    )
    parser.add_argument(
        "cobra_tools_path",
        help="Path to Cobra Tools"
    )
    parser.add_argument(
        "manifest",
        help="Path to the Manifest.xml file"
    )
    parser.add_argument(
        "-launch",
        action="store_true",
        help="If present, Planet Coaster 2 will launch"
    )
    parser.add_argument(
        "-appid",
        default=2688950,
        type=int,
        help="Steam App ID to launch (default is PC2)"
    )
    
    args = parser.parse_args()

    cobra_tools_path = Path(args.cobra_tools_path)
    if not cobra_tools_path.exists():
        print(f"Error: Cobra tools path not found: {cobra_tools_path}", file=sys.stderr)
        sys.exit(1)
    
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"Error: Manifest file not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)
    
    success = process_ovlpaths(cobra_tools_path, manifest_path)
    if success:
        if args.launch:
            subprocess.run(['start', f"steam://rungameid/{args.appid}"], shell=True, check=True)

    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
