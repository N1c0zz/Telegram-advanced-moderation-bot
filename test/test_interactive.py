#!/usr/bin/env python3
"""
Script di test interattivo per la moderazione del bot.
Digita messaggi e vedi se passerebbero nel bot o verrebbero bloccati.
"""

import sys
import os
import re
from dotenv import load_dotenv

# Aggiungi la directory src al path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.config_manager import ConfigManager
from src.logger_config import LoggingConfigurator
from src.moderation_rules import AdvancedModerationBotLogic

def _is_short_or_emoji_message_test(text: str) -> bool:
    """
    Replica la logica del bot reale, ma CORRETTA per bloccare parole inglesi.
    """
    clean_text = text.strip()
    
    # Lista di parole inglesi comuni che NON devono essere considerate "sicure"
    english_words = {
        'hi', 'hello', 'hey', 'how', 'are', 'you', 'what', 'where', 'when', 
        'why', 'who', 'can', 'could', 'would', 'should', 'will', 'thanks', 
        'thank', 'please', 'sorry', 'yes', 'no', 'okay', 'ok', 'bye', 'goodbye'
    }
    
    # Se è una parola inglese comune, NON è sicura
    if clean_text.lower() in english_words:
        return False
    
    # Messaggi MOLTO brevi (meno di 6 caratteri) E solo caratteri sicuri
    if len(clean_text) < 6:
        # Verifica che contenga solo caratteri latini/numeri/punteggiatura basic
        safe_pattern = r'^[a-zA-Z0-9\s.,!?;:()\-àèéìíîòóùú]*$'
        if re.match(safe_pattern, clean_text):
            return True
        else:
            # Contiene caratteri sospetti nonostante sia breve
            return False
    
    # Messaggi fino a 12 caratteri MA solo se pattern molto specifici e sicuri
    if len(clean_text) <= 12:
        safe_short_patterns = [
            r'^(si|no|ok|ciao|grazie|prego|bene|male|buono|ottimo|perfetto)$',  # Parole singole italiane
            r'^[0-9\s\-+/().,]+$',                       # Solo numeri e simboli
            r'^[.,!?;:\s]+$',                           # Solo punteggiatura
            r'^[👍👎❤️😊😢🎉✨🔥💪😍😂🤔😅]+$',            # Solo emoji comuni
        ]
        
        for pattern in safe_short_patterns:
            if re.match(pattern, clean_text.lower()):
                return True
    
    # Se arriva qui, NON è un messaggio breve sicuro
    return False

def test_message(moderation, message_text):
    """Testa un singolo messaggio e restituisce il risultato."""
    
    print(f"\n🔍 ANALISI MESSAGGIO: '{message_text}'")
    print("=" * 70)
    
    # Controlli che il messaggio breve/emoji (come nel bot reale)
    # CORREZIONE: Usa la stessa logica del bot reale
    is_short_safe = _is_short_or_emoji_message_test(message_text)
    if is_short_safe:
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
    
    # 2. NUOVO: Test controllo lingua di base (MANCAVA QUESTO!)
    is_disallowed_lang_basic = moderation.is_language_disallowed(message_text)
    
    if is_disallowed_lang_basic:
        print("❌ RISULTATO: BLOCCATO DAL CONTROLLO LINGUA DI BASE")
        print("   🌍 Il messaggio è in una lingua non consentita")
        print("   📝 Non passa nemmeno per l'analisi OpenAI")
        return
    
    print("✅ CONTROLLO LINGUA DI BASE: PASSATO")
    print("   📤 Il messaggio passa a OpenAI per analisi contestuale")
    
    # 3. Test analisi OpenAI (se disponibile)
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
                print("   🚫 Motivo: Lingua non consentita (rilevata da AI)")
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
        
        # Mostra configurazione lingue
        allowed_languages = config_manager.get('allowed_languages', ['italian'])
        print(f"🌍 Lingue consentite: {allowed_languages}")
        
        if not moderation.openai_client:
            print("⚠️  OpenAI non disponibile - verrà usato solo il filtro meccanico e controllo lingua locale")
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