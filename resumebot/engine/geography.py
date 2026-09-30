"""Recognize US ATS locations that omit their country."""
import re

US_STATES = dict(zip(
    'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split(),
    'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia'.split('|'), strict=True))


def is_us_location(location: str) -> bool:
    if re.search(r'\b(United States|USA|US)\b|\bU\.S\.(?:A\.)?', location, re.I):
        return True
    # Require a city/state separator to avoid e.g. Georgia (the country).
    states = '|'.join([*US_STATES, *US_STATES.values()])
    return bool(re.search(r',\s*(?:' + states + r')(?:\s+\d{5})?\s*$', location, re.I))
