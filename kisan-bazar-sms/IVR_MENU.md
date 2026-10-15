# IVR menu — 1618-style voice call (e.g. dial 16XX)

For farmers with no data connection and low text literacy — matches Nepal's
existing 1618 Krishi Call Center pattern, but automated for the common paths
and falling through to a human agent for anything else. Same four intents
as USSD and SMS, same crop vocabulary, so a co-op field agent teaches one
mental model across all three channels.

DTMF = the touch-tone digit the caller presses; DHM/co-op copy should be
recorded by a native speaker, not machine TTS, for the alert and confirm
prompts — trust in the *voice* matters as much as the words for a warning.

## Call flow

```
[Call connects]
  Play: "किसान बजारमा स्वागत छ। भाषाका लागि नेपालीको लागि १, अंग्रेजीको लागि २ थिच्नुहोस्।"
        ("Welcome to Kisan Bazar. For Nepali press 1, for English press 2.")
  DTMF 1 -> lang = ne, continue
  DTMF 2 -> lang = en, continue
  [no input 8s] -> repeat once, then default to ne and continue

[Root menu, language-appropriate]
  Play: "आजको भाउ सुन्न १, मौसम सुन्न २, आफ्नो उत्पादन बेच्न ३,
         आफ्नो हिसाब सुन्न ४, मान्छेसँग कुरा गर्न ० थिच्नुहोस्।"
        ("For today's rates press 1, for weather press 2,
          to sell your produce press 3, for your ledger press 4,
          to speak with a person press 0.")

  DTMF 1 -> RATES branch
  DTMF 2 -> WEATHER branch
  DTMF 3 -> SELL branch
  DTMF 4 -> LEDGER branch
  DTMF 0 -> transfer to co-op call desk during 6am-8pm; outside those hours,
            play hours and offer an SMS callback request instead
  [no input 8s, twice] -> "फेरि फोन गर्नुहोस्।" / "Please call again." -> hang up
```

## RATES branch

```
Play: "बाली छान्नुहोस्: काउलीको लागि १, गोलभेँडाको लागि २, आलुको लागि ३,
       प्याजको लागि ४, अदुवाको लागि ५, मकैको लागि ६, पछाडि जान ९।"
DTMF 1-6 -> Play: "<crop>: प्रति के.जी. रु <rate>। कालीमाटी थोक भाउ।"
            Then: "फेरि सुन्न १, मुख्य मेनुमा फर्कन ९।"
DTMF 9   -> back to root menu
```

## WEATHER branch

```
Look up caller's registered ward (matched by caller ID / phone number
against `users.phone`, same as SMS and USSD).

If an active warning/severe bulletin covers that ward:
  Play the bulletin's message_ne/message_en verbatim, at normal pace, twice.
  Then: "यो सूचना फेरि सुन्न १, मुख्य मेनुमा फर्कन ९।"
Else:
  Play: "अहिलेलाई तपाईंको क्षेत्रमा कुनै मौसम चेतावनी छैन।"
        ("No weather warning for your area right now.")
  Then: "मुख्य मेनुमा फर्कन ९ थिच्नुहोस्।"
```

Severe bulletins should also **proactively call** registered farmers in the
affected wards (outbound IVR blast), not wait for them to dial in — inbound
IVR covers the routine check, outbound covers the emergency.

## SELL branch

Voice is a poor fit for multi-field data entry (crop + kg + grade), and
misheard digits on a bad rural line are exactly where a wrong listing could
cost a farmer a wasted co-op trip. So this branch deliberately does the
minimum by voice and hands off the rest:

```
Play: "बेच्न SMS मा 'BECH' लेखेर पठाउनुहोस्, जस्तै BECH KAULI 40 A।
       वा पछि नै फोन राखेर हाम्रो मान्छेले कल फर्काउनेछ।
       अहिले नै मान्छेसँग कुरा गर्न ० थिच्नुहोस्।"
      ("To sell, text BECH to this number, e.g. BECH KAULI 40 A.
        Or hang up and our staff will call you back.
        To speak to someone right now press 0.")
DTMF 0 -> transfer to co-op call desk
[hang up] -> queue a callback task for co-op staff within 24h
```

## LEDGER branch

```
Play: "तपाईंको पछिल्लो दुई कारोबार:"
      ("Your last two transactions:")
For each of the 2 most recent entries:
  Play: "<crop> <kg> के.जी., <status>, रु <amount>।"
Then: "मुख्य मेनुमा फर्कन ९।"
```

## Design notes for whoever builds the IVR flow in the telephony platform

- Every branch ends by offering **9 for main menu**, never a dead end —
  a caller on a shared phone who gets confused should never have to
  redial from scratch.
- Numbers are read digit-by-digit in Nepali ("पाँच पाँच" not "पचपन्न") for
  rates and weights, since group-read numbers are easy to mishear on a
  poor connection — this should be a TTS/recording convention enforced
  across every branch, not just decided ad hoc per prompt.
- Keep each spoken menu under ~15 seconds; if it's not decidable in that
  time, the option probably belongs in the SMS/app instead of on this call.
- Log every DTMF path taken per call (not the audio, the path) — this is
  how you find out which menu option farmers actually use, and where they
  give up and press 0.
