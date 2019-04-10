# -*- coding: utf-8 -*-
"""CS690D - NMT Eval.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1brTZBFJCBsYOZ6UMmxVI7BZVi6EN2pSa
"""

!git clone https://github.com/moses-smt/mosesdecoder.git
!pip install sacrebleu
!pip install -U -q PyDrive

from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive
from google.colab import auth
from oauth2client.client import GoogleCredentials
# Authenticate and create the PyDrive client.
auth.authenticate_user()
gauth = GoogleAuth()
gauth.credentials = GoogleCredentials.get_application_default()
drive = GoogleDrive(gauth)
print('Authentication Successful!')
iwslt_data = drive.CreateFile({'id': '1Swpb8yG3atDzxiOR0dQuFE-3KjgZCuWE'})
iwslt_data.GetContentFile('iwslt_en_de.zip')

!unzip iwslt_en_de.zip dev.en

"""%%shell
#!/bin/bash

# This is a reference to the gold translations from the dev set
REFERENCE_FILE="dev.en"

# XXX: Change the following line to point to your model's output!
TRANSLATED_FILE="dev.en"

# The model output is expected to be in a tokenized form. Note, that if you
# tokenized your inputs to the model, then simply joined each output token with
# whitespace you should get tokenized outputs from your model.
# i.e. each output token is separate by whitespace
# e.g. "My model 's output is interesting ."
perl "mosesdecoder/scripts/tokenizer/detokenizer.perl" -l en < "$TRANSLATED_FILE" > "$TRANSLATED_FILE.detok"

PARAMS=("-tok" "intl" "-l" "de-en" "$REFERENCE_FILE")
sacrebleu "${PARAMS[@]}" < "$TRANSLATED_FILE.detok"
"""