import subprocess

def execute_shell(command: str) -> str:
    """
    Executes a shell command and returns the stdout merged with stderr.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[Errors/Warnings]:\n{result.stderr}"
        return output or "[Command executed with no output]"
    except subprocess.TimeoutExpired:
        return "[Error]: Command execution timed out after 15 seconds."
    except Exception as e:
        return f"[Error]: Failed to execute shell command: {str(e)}"

def execute_applescript(script_content: str) -> str:
    """
    Executes AppleScript code via osascript and returns output.
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', script_content],
            capture_output=True,
            text=True,
            timeout=10
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[Errors/Warnings]:\n{result.stderr}"
        return output or "[AppleScript executed successfully with no output]"
    except subprocess.TimeoutExpired:
        return "[Error]: AppleScript execution timed out after 10 seconds."
    except Exception as e:
        return f"[Error]: Failed to execute AppleScript: {str(e)}"

if __name__ == "__main__":
    # Quick sanity checks
    print("Testing shell execution...")
    uname_output = execute_shell("uname -a")
    print(f"Uname output: {uname_output}")
    
    print("Testing AppleScript execution...")
    volume_output = execute_applescript("get volume settings")
    print(f"Volume settings: {volume_output}")
