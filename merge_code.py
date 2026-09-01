import os


def merge_python_files(source_dir: str, output_file: str):
    """
    Scans the specified source directory for all .py files and merges their contents
    into a single Markdown document suitable for NotebookLM ingestion.
    """
    # Directory names to exclude from traversal
    ignore_dirs = {
        '.git',
        '.venv',
        'venv',
        '__pycache__',
        'build',
        'dist',
        '.idea',
        '.vscode',
    }

    # Resolve absolute paths for reliable file handling
    abs_source_dir = os.path.abspath(source_dir)
    abs_output_file = os.path.abspath(output_file)

    print(f"🔍 Scanning directory: {abs_source_dir}\n")

    file_count = 0
    with open(abs_output_file, 'w', encoding='utf-8') as outfile:
        outfile.write("# PROJECT CODEBASE OVERVIEW\n\n")

        for root, dirs, files in os.walk(abs_source_dir):
            # Prune ignored directories in-place
            dirs[:] = [d for d in dirs if d not in ignore_dirs]

            for file in files:
                file_path = os.path.join(root, file)

                # Skip output file if it happens to share a .py extension
                if file.endswith('.py') and (
                    os.path.abspath(file_path) != abs_output_file
                ):
                    rel_path = os.path.relpath(file_path, abs_source_dir)

                    print(f"  [+] Found: {rel_path}")
                    outfile.write(f"## File: `{rel_path}`\n\n")
                    outfile.write("```python\n")
                    try:
                        with open(file_path, 'r', encoding='utf-8') as infile:
                            outfile.write(infile.read())
                    except Exception as e:
                        outfile.write(f"# Error reading file: {e}\n")
                    outfile.write("\n```\n\n" + "-" * 40 + "\n\n")
                    file_count += 1

    print("\n" + "=" * 50)
    if file_count > 0:
        print(f"✅ Successfully merged {file_count} .py file(s)!")
        print(f"📁 Output file saved to: {abs_output_file}")
    else:
        print("⚠️ No .py files found! Please check your PROJECT_DIR path.")
    print("=" * 50)


if __name__ == "__main__":
    # Specify your target project directory:
    # Use "." if the script is placed inside the project root directory,
    # or provide an absolute path, e.g., r"C:\Projects\MyQuantProject"
    PROJECT_DIR = "."
    OUTPUT_FILE = "project_codebase.md"

    merge_python_files(PROJECT_DIR, OUTPUT_FILE)
