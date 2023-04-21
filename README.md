! [alt text] (https://comprarcriptomoedas.com/content/images/2021/03/Solana-Logo.png)
# Solana Transaction Explorer
This is a Python program that allows users to explore transactions on the Solana blockchain using the Solana API. The program provides a simple graphical user interface that allows users to enter a transaction ID and explore the details of that transaction, including the sender, recipient, and amount transferred.

# Features
- Explore transactions on the Solana blockchain using the Solana API
- View transaction details, including sender, recipient, and amount transferred
- Simple graphical user interface using the tkinter library
- Error handling to handle network errors and invalid input
- Input validation to ensure that the user enters a valid transaction ID
- Caching mechanism to avoid unnecessary API requests for previously requested transactions
- Batch processing feature to explore multiple transactions at once

# Requirements
To run this program, you will need:

- Python 3.x
- The requests library (pip install requests)
- The tkinter library (included with Python)


# Usage
- To use this program, simply run the solana_transaction_explorer.py file using Python. This will open a graphical user interface that allows you to explore transactions on the Solana blockchain.

- To explore a transaction, enter the transaction ID in the text box and click the "Explore Transaction" button. The program will make an API request to the Solana API and display the transaction details in a message box.

- To explore multiple transactions at once, enter the transaction IDs separated by commas in the text box and click the "Explore Transactions" button. The program will make API requests for each transaction ID and display the transaction details in a separate message box for each transaction.

# License
This program is licensed under the MIT License. See the LICENSE file for more information.

# Contributions
Contributions to this program are welcome. Please fork the repository and submit a pull request with your changes.
