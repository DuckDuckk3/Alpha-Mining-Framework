import os

def merge_python_files(source_dir, output_file):
    # List of ignored folders
    ignore_dirs = {'.git', '.venv', 'venv', '__pycache__', 'build', 'dist'}
    
    with open(output_file, 'w', encoding='utf-8') as outfile:
        outfile.write("# DỰ ÁN CODEBASE - TỔNG QUAN MÃ NGUỒN\n\n")
        
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            
            for file in files:
                if file.endswith('.py'):
                    file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(file_path, source_dir)
                    
                    outfile.write(f"## File: `{rel_path}`\n\n")
                    outfile.write("```python\n")
                    try:
                        with open(file_path, 'r', encoding='utf-8') as infile:
                            outfile.write(infile.read())
                    except Exception as e:
                        outfile.write(f"# Lỗi khi đọc file: {e}\n")
                    outfile.write("\n```\n\n" + "-"*40 + "\n\n")

if __name__ == "__main__":
    merge_python_files(".", "project_codebase.md")
    print("Merged all into file 'project_codebase.md'!")
