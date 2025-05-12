#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Moderation Bot - Main Entry Point
"""
import os
import signal
import sys

from src.bot import TelegramModerationBot
from src.logging_setup import LoggingConfigurator

def main():
    """Main entry point for the bot"""
    # Mostra la directory di lavoro corrente
    print(f"Directory di lavoro corrente: {os.getcwd()}")
    
    # Configurazione del logging
    logger = LoggingConfigurator.setup_logging(disable_console=True)
    
    # Gestore segnali per spegnimento pulito
    def signal_handler(signum, frame):
        logger.info("\n🛑 Arresto in corso...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Inizializzazione e avvio del bot
    bot = TelegramModerationBot()
    bot.start()

if __name__ == "__main__":
    main()