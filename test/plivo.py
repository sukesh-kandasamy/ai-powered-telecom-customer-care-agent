import plivo

client = plivo.RestClient('MAOTVLNMY5NZETZDU2MI', 'NzRhNWEwNTYtMTkxOC00N2Q0LTRlMmUtNWZiM2Nj')

response = client.calls.create(
    from_='+14151234567',  # Your Plivo number
    to_='+919600944093',    # Destination number
    answer_url='https://s3.amazonaws.com/static.plivo.com/answer.xml',
    answer_method='GET'
)

print(response)