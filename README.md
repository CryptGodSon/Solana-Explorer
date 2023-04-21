# Solana-Explorer
Python program that uses the Solana API to explore transactions in the Solana blockchain


# How to use ?
To use this program, you'll need to replace the INSERT_TRANSACTION_ID_HERE placeholder with the ID of the transaction you want to explore. You can find transaction IDs on Solana block explorers like Solscan or Solana Beach.

When you run the program, it will print out the sender, recipient, and amount of the specified transaction. You can modify the program to explore other details of the transaction or to explore transactions in a different way.

Note that this program assumes you're exploring transactions on the Solana mainnet. If you're exploring transactions on a testnet, you'll need to use a different Solana RPC endpoint.

# Python Version

This code should work with the latest version of Python (as of my knowledge cutoff in September 2021). However, if you encounter any errors or issues, let me know and I'll do my best to help you troubleshoot them!


# GUI 
To create a front-end executable for this program, you can use a Python library like PyInstaller or cx_Freeze to package the code into a standalone executable file.

To create an executable file from this code using PyInstaller, you can run the following command in your terminal:
------------
pyinstaller --onefile --windowed <filename>.py
------------

This will create a standalone executable file that can be run on any computer without requiring Python or any dependencies to be installed.
