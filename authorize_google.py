"""Run on the user's own computer to authorize the standalone service."""
import argparse
import json
import os
from pathlib import Path

def main():
    from google_auth_oauthlib.flow import InstalledAppFlow
    parser=argparse.ArgumentParser()
    parser.add_argument('--client',required=True,help='Google Desktop OAuth client JSON')
    parser.add_argument('--output',default='token.json')
    parser.add_argument('--calendar',action='store_true')
    args=parser.parse_args()
    dest=Path(args.output)
    if dest.exists() or dest.is_symlink():
        parser.error('Choose a new output path; existing credentials will not be overwritten')
    scopes=['https://www.googleapis.com/auth/drive.file',
            'https://www.googleapis.com/auth/gmail.send',
            'https://www.googleapis.com/auth/gmail.metadata']
    if args.calendar:
        scopes += ['https://www.googleapis.com/auth/calendar.events',
                   'https://www.googleapis.com/auth/calendar.readonly']
    flow=InstalledAppFlow.from_client_secrets_file(args.client,scopes)
    creds=flow.run_local_server(port=0,access_type='offline',prompt='consent')
    if not creds.refresh_token:
        raise RuntimeError('No refresh token returned')
    os.umask(0o077)
    with dest.open('x') as f:
        f.write(creds.to_json())
    print('Google authorization saved. No credentials displayed.')

if __name__=='__main__':
    main()
