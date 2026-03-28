"""Run this to see exactly what bytes arrive for each key press.
   python key_test.py
   Press Up, Down, Esc, Enter — then Ctrl+C to quit.
"""
import msvcrt, sys

print("Press keys (Ctrl+C to quit)...")
print()
while True:
    ch = msvcrt.getch()
    print(f"  getch() -> {ch!r}  hex={ch.hex()}", end="")
    if ch in (b'\x00', b'\xe0'):
        ch2 = msvcrt.getch()
        print(f"  +  {ch2!r}  hex={ch2.hex()}", end="")
    elif ch == b'\x1b':
        import time; time.sleep(0.05)
        extra = b''
        while msvcrt.kbhit():
            extra += msvcrt.getch()
        if extra:
            print(f"  +  {extra!r}  hex={extra.hex()}", end="")
    print()
    if ch == b'\x03':
        break
