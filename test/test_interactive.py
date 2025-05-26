#!/usr/bin/env python3
"""
Script di test interattivo per la moderazione del bot.
Digita messaggi e vedi se passerebbero nel bot o verrebbero bloccati.
"""

import sys
import os
from dotenv import load_dotenv

# Aggiungi la directory src al path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.config_manager import ConfigManager
from src.logger_config import LoggingConfigurator
from src.moderation_rules import AdvancedModerationBotLogic

def test_message(moderation, message_text):
    """Testa un singolo messaggio e restituisce il risultato."""
    
    print(f"\n🔍 ANALISI MESSAGGIO: '{message_text}'")
    print("=" * 70)
    
    # Controlli che il messaggio breve/emoji (come nel bot reale)
    if len(message_text.strip()) < 10:
        normalized = moderation.normalize_text(message_text)
        letters_only = ''.join(c for c in normalized if c.isalpha())
        if len(letters_only) < 5:
            print("✅ RISULTATO: APPROVATO (messaggio breve/emoji)")
            print("   🔹 Motivo: Saltata analisi AI per messaggio troppo breve")
            return
    
    # 1. Test filtro meccanico
    is_blocked_mechanical = moderation.contains_banned_word(message_text)
    
    if is_blocked_mechanical:
        print("❌ RISULTATO: BLOCCATO DAL FILTRO MECCANICO")
        print("   🚫 Il messaggio contiene spam ovvio e viene eliminato immediatamente")
        print("   📝 Non passa nemmeno per l'analisi OpenAI")
        return
    
    print("✅ FILTRO MECCANICO: PASSATO")
    print("   📤 Il messaggio passa a OpenAI per analisi contestuale")
    
    # 2. Test analisi OpenAI (se disponibile)
    try:
        is_inappropriate_ai, is_question_ai, is_disallowed_lang_ai = moderation.analyze_with_openai(message_text)
        
        print(f"\n🤖 ANALISI OPENAI:")
        print(f"   📋 Inappropriato: {'SI' if is_inappropriate_ai else 'NO'}")
        print(f"   ❓ È una domanda: {'SI' if is_question_ai else 'NO'}")
        print(f"   🌍 Lingua consentita: {'NO' if is_disallowed_lang_ai else 'SI'}")
        
        # Risultato finale
        if is_inappropriate_ai or is_disallowed_lang_ai:
            print(f"\n❌ RISULTATO FINALE: BLOCCATO DA OPENAI")
            if is_inappropriate_ai:
                print("   🚫 Motivo: Contenuto inappropriato rilevato da AI")
            if is_disallowed_lang_ai:
                print("   🚫 Motivo: Lingua non consentita")
        else:
            print(f"\n✅ RISULTATO FINALE: APPROVATO")
            print("   ✨ Il messaggio verrebbe accettato nel gruppo")
            
    except Exception as e:
        print(f"\n⚠️  ERRORE OPENAI: {e}")
        print("   🔄 Il bot userebbe il fallback locale (probabilmente approvato)")
        print("   ✅ RISULTATO FINALE: PROBABILMENTE APPROVATO")

def main():
    """Funzione principale del test interattivo."""
    
    print("🤖 TEST INTERATTIVO MODERAZIONE BOT")
    print("=" * 50)
    print("Digita messaggi per vedere se passerebbero nel tuo bot.")
    print("Comandi speciali:")
    print("  • 'quit' o 'exit' per uscire")
    print("  • 'help' per vedere di nuovo questi comandi")
    print("=" * 50)
    
    # Carica configurazione (disabilita log su console per pulire l'output)
    load_dotenv()
    config_manager = ConfigManager()
    logger = LoggingConfigurator.setup_logging(disable_console=True)  # Log solo su file
    
    try:
        moderation = AdvancedModerationBotLogic(config_manager, logger)
        print("✅ Sistema di moderazione inizializzato correttamente")
        
        if not moderation.openai_client:
            print("⚠️  OpenAI non disponibile - verrà usato solo il filtro meccanico")
        else:
            print("🤖 OpenAI configurato - analisi completa disponibile")
            
    except Exception as e:
        print(f"❌ Errore inizializzazione: {e}")
        print("Controlla la configurazione e riprova.")
        return
    
    print("\n" + "=" * 50)
    
    while True:
        try:
            # Input dall'utente
            message = input("\n💬 Digita un messaggio da testare: ").strip()
            
            # Comandi speciali
            if message.lower() in ['quit', 'exit', 'q']:
                print("👋 Ciao!")
                break
            elif message.lower() in ['help', 'h']:
                print("\n📖 COMANDI DISPONIBILI:")
                print("  • Digita qualsiasi messaggio per testarlo")
                print("  • 'quit' o 'exit' per uscire")
                print("  • 'help' per vedere questi comandi")
                continue
            elif not message:
                print("⚠️  Messaggio vuoto, riprova.")
                continue
            
            # Test del messaggio
            test_message(moderation, message)
            
        except KeyboardInterrupt:
            print("\n\n👋 Interrotto dall'utente. Ciao!")
            break
        except Exception as e:
            print(f"\n❌ Errore durante il test: {e}")
            print("Riprova con un altro messaggio.")

if __name__ == "__main__":
    main()