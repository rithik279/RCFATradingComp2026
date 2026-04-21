import os
import functools
import operator
import itertools
from time import sleep
import signal
import requests

class ApiException(Exception):
    pass

# this signal handler allows for a graceful shutdown when CTRL+C is pressed
def signal_handler(signum, frame):
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True

# set your API key to authenticate to the RIT client
API_KEY = {'X-API-Key': 'YOUR API KEY HERE'}
shutdown = False

# this helper method returns the current 'tick' of the running case
def get_tick(session):
    resp = session.get('http://localhost:9999/v1/case')
    if resp.status_code == 401:
        raise ApiException('The API key provided in this Python code must match that in the RIT client (please refer to the API hyperlink in the client toolbar and/or the RIT – User Guide – REST API Documentation.pdf)')
    case = resp.json()
    return case['tick']

# this helper method builds the depth view for two tickers
def depth_view(session):
    crzy_resp = session.get('http://localhost:9999/v1/securities/book?ticker=CRZY')
    tame_resp = session.get('http://localhost:9999/v1/securities/book?ticker=TAME')
    if crzy_resp.status_code == 401 or tame_resp.status_code == 401:
        raise ApiException('The API key provided in this Python code must match that in the RIT client (please refer to the API hyperlink in the client toolbar and/or the RIT – User Guide – REST API Documentation.pdf)')
    crzy_book = crzy_resp.json()
    tame_book = tame_resp.json()
    calculate_cumulatives(crzy_book['bids'])
    calculate_cumulatives(crzy_book['asks'])
    calculate_cumulatives(tame_book['bids'])
    calculate_cumulatives(tame_book['asks'])
    combined = itertools.zip_longest(crzy_book['bids'], crzy_book['asks'], tame_book['bids'], tame_book['asks'], fillvalue={'cumulative_vwap': 0, 'cumulative_vol': 0, 'price': 0})
    return combined

# this helper method calculates cumulative volumes and VWAPs for each level of an order book
def calculate_cumulatives(book):
    for level in book:
        slice = book[:book.index(level) + 1]
        level['cumulative_vol'] = int(sum(s['quantity'] - s['quantity_filled'] for s in slice))
        level['cumulative_vwap'] = sum(functools.reduce(operator.mul, data) for data in zip((s['quantity'] - s['quantity_filled'] for s in slice), (s['price'] for s in slice))) / level['cumulative_vol']

# this helper method prints two order books to the screen
def print_books(combined):
    os.system('cls')
    print('CRZY                                                           TAME')
    print('BIDVWAP | CUMUVOL |  BID  |  ASK  | CUMUVOL | ASKVWAP          BIDVWAP | CUMUVOL |  BID  |  ASK  | CUMUVOL | ASKVWAP')
    for level in itertools.islice(combined, 20):
        crzy_bid, crzy_ask, tame_bid, tame_ask = level
        print('{:07.4f} | {:07d} | {:05.2f} | {:05.2f} | {:07d} | {:07.4f}          {:07.4f} | {:07d} | {:05.2f} | {:05.2f} | {:07d} | {:07.4f}'.format(crzy_bid.get('cumulative_vwap'), crzy_bid.get('cumulative_vol'), crzy_bid.get('price'), crzy_ask.get('price'), crzy_ask.get('cumulative_vol'), crzy_ask.get('cumulative_vwap'), tame_bid.get('cumulative_vwap'), tame_bid.get('cumulative_vol'), tame_bid.get('price'), tame_ask.get('price'), tame_ask.get('cumulative_vol'), tame_ask.get('cumulative_vwap')))
    sleep(0.5)

# this is the main method containing the actual order routing logic
def main():
    # creates a session to manage connections and requests to the RIT Client
    with requests.Session() as s:
        # add the API key to the session to authenticate during requests
        s.headers.update(API_KEY)
        # get the current time of the case
        tick = get_tick(s)

        # while the time is <= 300
        while tick <= 300:
            # get and print the two books to the prompt
            books = depth_view(s)
            print_books(books)

            # refresh the case time. THIS IS IMPORTANT FOR THE WHILE LOOP
            tick = get_tick(s)

# this calls the main() method when you type 'python lt3.py' into the command prompt
if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    main()
