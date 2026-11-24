# Pipeline description (in my own words)

I built a voice receptionist for a clinic. When you click "Try Call" in the
dashboard, your browser opens a direct WebRTC connection to my Python server —
no phone provider in the middle. Your voice travels through a Pipecat pipeline:
ElevenLabs turns speech into text, Gemini (the free tier) decides what to do
and what to say, and ElevenLabs turns its reply back into speech. Silero VAD
sits in the pipeline so the agent knows when you have finished speaking and
when you interrupt it.

The interesting part is not the models, it is the tools. Gemini does not book
anything by itself — it calls three functions I wrote: one to check a doctor's
free slots, one to book, one to cancel. All three read and write the same
SQLite database the dashboard reads, so the moment the agent books, the
appointment appears on the screen. The caller is identified only by the phone
number they say out loud; the agent reads it back and waits for a yes before
booking or cancelling, because a wrong number here would book the wrong person.

I used AI tools to write a lot of this, and to check the Pipecat 1.17 API,
which has changed names a few times. I tested the booking logic end to end
through the same functions the voice agent calls, and I kept a text mode in
the phone so the flow can be tried even without API keys — that mode is a
small state machine, not the LLM, and I have said so in the README rather
than pretending it is the voice pipeline.

What I would improve next: real calendar sync instead of mock slots, and a
proper phone number (Twilio) so real patients can call in, instead of only
from the browser.
