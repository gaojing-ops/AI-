# -*- coding: utf-8 -*-
"""Double-click launcher for the desktop GUI."""

import tkinter as tk

from gui_app import NovelGeneratorGUI


def main():
    root = tk.Tk()
    NovelGeneratorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
