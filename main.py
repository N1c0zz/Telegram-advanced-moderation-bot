#!/usr/bin/env python3
import os
import sys
import signal
import logging
from dotenv import load_dotenv
from src.telegram_bot import TelegramModerationBot

def main():
    # Mostra la directory di lavoro corrente
    print(f"Directory di lavoro corrente: {os.getcwd()}")
    
    # Gestore segnali per spegnimento pulito
    def signal_handler(signum, frame):
        print("\n🛑 Arresto in corso...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Carica variabili d'ambiente
    load_dotenv()
    
    # Avvia il bot
    bot = TelegramModerationBot()
    bot.start()

if __name__ == "__main__":
    main()