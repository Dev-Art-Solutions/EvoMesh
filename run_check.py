import subprocess

result = subprocess.run(["python", "check_reach.py"], capture_output=True, text=True)
print("STDOUT:")
print(result.stdout)
print("STDERR:")
print(result.stderr)
