import platform
import sys


def main() -> None:
    print("Hello, world!")
    print(f"Python: {sys.version.splitlines()[0]}")
    print(f"Platform: {platform.platform()}")
    print(f"Machine: {platform.machine()}")


if __name__ == "__main__":
    main()
