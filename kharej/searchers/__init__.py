"""Platform search adapters for the Kharej VPS worker.

Each searcher module exposes an ``async`` function that takes a query string
and a limit, performs the search using the appropriate backend, and returns
a structured result ready to be packed into a :class:`~kharej.contracts.SearchResult`
message.
"""
