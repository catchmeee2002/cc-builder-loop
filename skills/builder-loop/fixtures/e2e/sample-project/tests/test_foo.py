import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from foo import add, greet


def test_add():
    assert add(1, 2) == 3
    assert add(-1, 1) == 0
    assert add(0, 0) == 0


def test_greet():
    assert greet("World") == "Hello, World!"
    assert greet("") == "Hello, !"
