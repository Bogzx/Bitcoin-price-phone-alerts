from dotenv import load_dotenv
import os
from twilio.rest import Client
load_dotenv()

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_phone_number = os.getenv("TWILIO_PHONE_NUMBER")
client = Client(account_sid, auth_token)


def apeleaza(numartel, mesaj):
    call = client.calls.create(
        twiml=f'''
            <Response>
                <Say>
                    <prosody rate="slow">{mesaj}</prosody> 
                    <prosody rate="medium"> This was B.G.D trading. </prosody>
                </Say>
            </Response>
        ''',
        to=numartel,
        from_=twilio_phone_number
    )
    call

if __name__=="__main__":
    apeleaza(+40735810974,"Test test, acesta este un test.")