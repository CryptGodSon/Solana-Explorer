import requests

# Set the Solana RPC endpoint
solana_endpoint = 'https://api.mainnet-beta.solana.com'

# Set the transaction ID you want to explore
transaction_id = 'INSERT_TRANSACTION_ID_HERE'

# Make a request to the Solana API to get the transaction details
response = requests.get(f'{solana_endpoint}/v1/transactions/{transaction_id}')

# Check if the request was successful
if response.status_code == 200:
    # Get the transaction details from the response
    transaction = response.json()
    
    # Print the transaction details
    print(f'Transaction ID: {transaction_id}')
    print(f'Sender: {transaction["transaction"]["signatures"][0]["publicKey"]}')
    print(f'Recipient: {transaction["transaction"]["message"]["accountKeys"][1]}')
    print(f'Amount: {transaction["transaction"]["message"]["instructions"][0]["parsed"]["info"]["lamports"]} lamports')
else:
    print(f'Error: {response.status_code}')
