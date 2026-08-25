"""The web UI: a service layer, and a stdlib HTTP adapter over it.

`service.Service` holds every operation the UI performs and knows nothing
about HTTP. `handler` is the only module that touches request and response
objects, which is what would let a different front end - Django, say - reuse
the service unchanged.
"""


