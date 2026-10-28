#!/usr/bin/env python3
"""Type text into a libvirt VM's focused window via virtual key presses (US layout).

Usage: vmtype.py VM [text]   (text from stdin when omitted; \\n or a newline presses Enter)
Run as a libvirt group member, e.g.: sg libvirt -c "python3 vmtype.py HYD-DC1 'dir'"
"""
import subprocess
import sys

PLAIN = {' ': 'KEY_SPACE', '-': 'KEY_MINUS', '=': 'KEY_EQUAL', '[': 'KEY_LEFTBRACE', ']': 'KEY_RIGHTBRACE',
         '\\': 'KEY_BACKSLASH', ';': 'KEY_SEMICOLON', "'": 'KEY_APOSTROPHE', '`': 'KEY_GRAVE',
         ',': 'KEY_COMMA', '.': 'KEY_DOT', '/': 'KEY_SLASH', '\n': 'KEY_ENTER', '\t': 'KEY_TAB'}
SHIFTED = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
           '_': '-', '+': '=', '{': '[', '}': ']', '|': '\\', ':': ';', '"': "'", '~': '`',
           '<': ',', '>': '.', '?': '/'}


def keys(c):
    if c.isalpha():
        return (['KEY_LEFTSHIFT'] if c.isupper() else []) + ['KEY_' + c.upper()]
    if c.isdigit():
        return ['KEY_' + c]
    if c in PLAIN:
        return [PLAIN[c]]
    if c in SHIFTED:
        return ['KEY_LEFTSHIFT'] + keys(SHIFTED[c])
    raise SystemExit(f'cannot type {c!r}')


vm = sys.argv[1]
text = sys.argv[2].replace('\\n', '\n') if len(sys.argv) > 2 else sys.stdin.read()
for c in text:
    subprocess.run(['virsh', '-q', '-c', 'qemu:///system', 'send-key', vm, '--holdtime', '30', *keys(c)],
                   check=True, stdout=subprocess.DEVNULL)
