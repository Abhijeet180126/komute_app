"""Responses shaped from the field names in Apple's Maps Server API documentation. Not recorded from live calls."""

PLACE = {
    "id": "I1", "name": "100 Queen St W", "countryCode": "CA", "country": "Canada",
    "coordinate": {"latitude": 43.6528, "longitude": -79.3836},
    "formattedAddressLines": ["100 Queen St W", "Toronto ON M5H 2N1", "Canada"],
    "structuredAddress": {"subThoroughfare": "100", "thoroughfare": "Queen St W", "fullThoroughfare": "100 Queen St W",
                          "locality": "Toronto", "administrativeArea": "Ontario", "administrativeAreaCode": "ON", "postCode": "M5H 2N1"},
}
GEOCODE = {"results": [PLACE, {"id": "bad", "name": "no coordinate"}]}
REVERSE = {"results": [PLACE]}
SEARCH = {"results": [PLACE]}

COMPLETION = "/v1/search?q=100%20Queen%20St%20W&userLocation=43.65%2C-79.38&metadata=abc123"
AUTOCOMPLETE = {"results": [
    {"displayLines": ["100 Queen St W", "Toronto, ON, Canada"], "location": {"latitude": 43.6528, "longitude": -79.3836},
     "completionUrl": COMPLETION, "structuredAddress": PLACE["structuredAddress"]},
    {"displayLines": ["Tim Hortons", "Toronto, ON, Canada"], "completionUrl": "/v1/search?q=Tim%20Hortons&metadata=x"},
    {"displayLines": ["no url"]},
    {"displayLines": ["elsewhere"], "completionUrl": "https://evil.example/v1/search?q=x"},
    {"displayLines": ["other api"], "completionUrl": "/v1/token"},
]}

DIRECTIONS_A = {
    "routes": [{"distanceMeters": 2000, "durationSeconds": 240, "stepIndexes": [0, 1]}],
    "steps": [{"stepPathIndex": 0}, {"stepPathIndex": 1}],
    "stepPaths": [[{"latitude": 43.6, "longitude": -79.4}, {"latitude": 43.61, "longitude": -79.39}],
                  [{"latitude": 43.61, "longitude": -79.39}, {"latitude": 43.62, "longitude": -79.38}]],
}
DIRECTIONS_B = {
    "routes": [{"distanceMeters": 3000, "durationSeconds": 360, "stepIndexes": [0]}],
    "steps": [{"stepPathIndex": 0}],
    "stepPaths": [[{"latitude": 43.62, "longitude": -79.38}, {"latitude": 43.63, "longitude": -79.37}]],
}

ETAS_FULL = {"etas": [
    {"destination": {"latitude": 43.61, "longitude": -79.39}, "distanceMeters": 1000, "expectedTravelTimeSeconds": 125, "staticTravelTimeSeconds": 120},
    {"destination": {"latitude": 43.62, "longitude": -79.38}},
]}
