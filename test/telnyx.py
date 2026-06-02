from telnyx import Telnyx

client = Telnyx(
    api_key="KEY019E870416D6972D4C25FA368B30041E_uPZyr8Rt1JCBa8CE75wKBA",  # This is the default and can be omitted
)
response = client.calls.dial(
    connection_id="2973226339529655329",
    from_="+17792860160",
    to="+919600944093",
)
print(response.data)