#!/usr/bin/env python3
"""
Script di test interattivo per la moderazione del bot.
Digita messaggi e vedi se passerebbero nel bot o verrebbero bloccati.
FINALE: Include tutte le correzioni per banned words e link esterni.
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

def test_message(moderation, config_manager, message_text):
    """Testa un singolo messaggio seguendo ESATTAMENTE la logica corretta del bot reale."""
    
    print(f"\n🔍 ANALISI MESSAGGIO: '{message_text}'")
    print("=" * 70)
    
    # ===== AUTO-APPROVAZIONE IMMEDIATA =====
    
    # 1. Messaggi molto brevi (≤4 caratteri)
    auto_approve_short = config_manager.get('auto_approve_short_messages', True)
    short_max_length = config_manager.get('short_message_max_length', 4)
    
    if auto_approve_short and len(message_text.strip()) <= short_max_length:
        print("✅ RISULTATO: AUTO-APPROVATO (messaggio molto breve)")
        print(f"   📏 Lunghezza: {len(message_text.strip())} ≤ {short_max_length} caratteri")
        print("   🚀 Salta tutti i controlli per brevità")
        return
    
    # 2. Whitelist critica (solo parole/pattern molto specifici)
    if hasattr(moderation, 'contains_whitelist_word') and moderation.contains_whitelist_word(message_text):
        print("✅ RISULTATO: AUTO-APPROVATO (whitelist critica)")
        print("   🎯 Il messaggio contiene parole della whitelist critica")
        print("   🚀 Salta tutti i controlli per whitelist")
        return
    
    print("➡️  Nessuna auto-approvazione, procedo con controlli di sicurezza...")
    
    # ===== CONTROLLI DI SICUREZZA PRIORITARI (NON POSSONO ESSERE SALTATI) =====
    
    # 3. Test filtro diretto (parole bannate) - MIGLIORATO
    print("\n🔍 CONTROLLO FILTRO DIRETTO (parole bannate + pattern spam)...")
    print("   🔍 Controllo parole bannate dalla configurazione...")
    print("   🔍 Controllo pattern link Telegram + materiale...")
    print("   🔍 Controllo pattern spam mascherato...")
    
    is_blocked_mechanical = moderation.contains_banned_word(message_text)
    
    if is_blocked_mechanical:
        print("❌ RISULTATO: BLOCCATO DAL FILTRO DIRETTO")
        print("   🚫 Il messaggio contiene:")
        print("      • Parole esplicitamente bannate, O")
        print("      • Pattern di spam (link esterni + materiale), O") 
        print("      • Caratteri non-latini (cirillico), O")
        print("      • Pattern di spam mascherato")
        print("   📝 Eliminazione immediata, nessuna analisi AI")
        
        # DEBUG: Mostra dettagli del match
        banned_words = config_manager.get('banned_words', [])
        text_lower = message_text.lower()
        found_banned = [word for word in banned_words if word.lower() in text_lower]
        if found_banned:
            print(f"   🎯 Parole bannate trovate: {found_banned}")
        
        # Controllo link telegram
        telegram_patterns = [r'(?:https?://)?(?:t\.me|telegram\.me)/\w+', r'@\w+']
        has_telegram_link = any(re.search(pattern, text_lower, re.IGNORECASE) for pattern in telegram_patterns)
        material_words = ['panieri', 'riassunti', 'appunti', 'materiale', 'slides']
        has_material = any(word in text_lower for word in material_words)
        invitation_words = ['iscriversi', 'canale', 'link', 'sotto', 'sopra', 'clicca']
        has_invitation = any(word in text_lower for word in invitation_words)
        
        if has_telegram_link and has_material and has_invitation:
            print(f"   🎯 Pattern spam rilevato: Link Telegram + Materiale + Invito")
        
        return
    
    print("✅ FILTRO DIRETTO: PASSATO")
    print("   ✓ Nessuna parola bannata trovata")
    print("   ✓ Nessun pattern di spam rilevato")
    
    # 4. Test controllo lingua di base - MIGLIORATO
    print("\n🔍 CONTROLLO LINGUA DI BASE (algoritmo migliorato)...")
    print("   🔍 Controllo alfabeti non-latini...")
    print("   🔍 Controllo whitelist italiana estesa...")
    print("   🔍 Controllo pattern morfologici italiani...")
    print("   🔍 Controllo blocco inglese selettivo...")
    print("   🔍 Controllo Langdetect (solo testi lunghi ≥15 caratteri)...")
    
    is_disallowed_lang_basic = moderation.is_language_disallowed(message_text)
    
    if is_disallowed_lang_basic:
        print("❌ RISULTATO: BLOCCATO DAL CONTROLLO LINGUA DI BASE")
        print("   🌍 Il messaggio è in una lingua non consentita")
        print("   📝 Eliminazione immediata, nessuna analisi AI")
        
        # DEBUG: Analisi lingua dettagliata
        total_alpha = len([c for c in message_text if c.isalpha()])
        cyrillic_chars = sum(1 for char in message_text if '\u0400' <= char <= '\u04FF')
        if cyrillic_chars > 0:
            print(f"   🎯 Caratteri cirillici: {cyrillic_chars}/{total_alpha}")
        
        print(f"   📏 Lunghezza alfabetica: {total_alpha} caratteri")
        if total_alpha >= 15:
            print("   🤖 Abbastanza lungo per analisi Langdetect")
        else:
            print("   📏 Troppo breve per Langdetect, controlli base applicati")
        
        return
    
    print("✅ CONTROLLO LINGUA DI BASE: PASSATO")
    print("   ✓ Lingua riconosciuta come consentita")
    print("➡️  Controlli di sicurezza superati, procedo con analisi avanzata...")
    
    # ===== CONTROLLO MESSAGGI BREVI/EMOJI (SKIP ANALISI AI COSTOSA) =====
    print("\n🔍 CONTROLLO MESSAGGI BREVI/EMOJI (skip AI)...")
    
    def _is_short_or_emoji_message_updated(text: str) -> bool:
        """Replica della logica aggiornata del bot reale."""
        import re
        
        clean_text = text.strip()
        
        # Lista di parole inglesi comuni che NON devono essere considerate "sicure"
        english_words = {
            'hi', 'hello', 'hey', 'how', 'are', 'you', 'what', 'where', 'when', 
            'why', 'who', 'can', 'could', 'would', 'should', 'will', 'thanks', 
            'thank', 'please', 'sorry', 'yes', 'no', 'okay', 'ok', 'bye', 'goodbye',
            'good', 'bad', 'nice', 'great', 'welcome', 'see', 'the', 'and', 'but'
        }
        
        # Se è una parola inglese comune, NON è sicura
        if clean_text.lower() in english_words:
            return False
        
        # Messaggi brevi ma più lunghi della soglia auto-approvazione
        short_max_length = config_manager.get('short_message_max_length', 4)
        if len(clean_text) > short_max_length and len(clean_text) < 10:
            safe_pattern = r'^[a-zA-Z0-9\s.,!?;:()\-àèéìíîòóùú]*$'
            if re.match(safe_pattern, clean_text):
                return True
            else:
                return False
        
        # Messaggi fino a 15 caratteri con pattern specifici sicuri
        if len(clean_text) <= 15:
            safe_short_patterns = [
                r'^(si|no|ok|ciao|grazie|prego|bene|male|buono|ottimo|perfetto)$',
                r'^[0-9\s\-+/().,]+$',
                r'^[.,!?;:\s]+$',
                r'^[👍👎❤️😊😢🎉✨🔥💪😍😂🤔😅]+$',
            ]
            
            for pattern in safe_short_patterns:
                if re.match(pattern, clean_text.lower()):
                    return True
        
        return False
    
    is_short_safe = _is_short_or_emoji_message_updated(message_text)
    if is_short_safe:
        print("✅ RISULTATO: APPROVATO (messaggio breve/emoji sicuro)")
        print("   🔹 Motivo: Passa controlli sicurezza, skip analisi AI costosa")
        print("   📝 Messaggio sicuro ma non abbastanza breve per auto-approvazione")
        return
    
    print("➡️  Messaggio necessita analisi AI completa...")
    
    # ===== ANALISI AI COMPLETA =====
    print("\n🤖 ANALISI OPENAI (con prompt aggiornato)...")
    print("   🆕 Nuovo: Rileva link esterni + materiale come inappropriato")
    print("   🆕 Nuovo: Esempi specifici del tuo caso spam")
    
    try:
        is_inappropriate_ai, is_question_ai, is_disallowed_lang_ai = moderation.analyze_with_openai(message_text)
        
        print(f"   📋 Inappropriato: {'SI' if is_inappropriate_ai else 'NO'}")
        print(f"   ❓ È una domanda: {'SI' if is_question_ai else 'NO'}")
        print(f"   🌍 Lingua consentita: {'NO' if is_disallowed_lang_ai else 'SI'}")
        
        # Risultato finale
        if is_inappropriate_ai or is_disallowed_lang_ai:
            print(f"\n❌ RISULTATO FINALE: BLOCCATO DA OPENAI")
            if is_inappropriate_ai:
                print("   🚫 Motivo: Contenuto inappropriato rilevato da AI")
                print("      • Potrebbe essere: spam mascherato, link esterni, vendita, etc.")
            if is_disallowed_lang_ai:
                print("   🚫 Motivo: Lingua non consentita (rilevata da AI)")
        else:
            print(f"\n✅ RISULTATO FINALE: APPROVATO")
            print("   ✨ Il messaggio verrebbe accettato nel gruppo")
            if is_question_ai:
                print("   ❓ Inoltre: Classificato come domanda")
            
    except Exception as e:
        print(f"\n⚠️  ERRORE OPENAI: {e}")
        print("   🔄 Il bot userebbe il fallback locale")
        print("   ✅ RISULTATO FALLBACK: APPROVATO")
        print("   📝 I controlli di sicurezza locali sono già stati superati")

def show_config_info(config_manager, moderation):
    """Mostra informazioni sulla configurazione corrente."""
    print("\n📋 CONFIGURAZIONE ATTUALE:")
    print("=" * 50)
    
    # Lingue consentite
    allowed_languages = config_manager.get('allowed_languages', ['italian'])
    print(f"🌍 Lingue consentite: {allowed_languages}")
    
    # Auto-approvazione messaggi brevi
    auto_approve_short = config_manager.get('auto_approve_short_messages', True)
    short_max_length = config_manager.get('short_message_max_length', 4)
    print(f"📏 Auto-approvazione messaggi brevi: {'✅' if auto_approve_short else '❌'}")
    if auto_approve_short:
        print(f"   └─ Lunghezza massima: ≤{short_max_length} caratteri")
    
    # Whitelist critica
    whitelist_words = config_manager.get('whitelist_words', [])
    print(f"🎯 Whitelist critica: {len(whitelist_words)} parole configurate")
    if whitelist_words:
        print(f"   └─ Parole: {', '.join(whitelist_words)}")
    else:
        print("   └─ Nessuna parola configurata")
    
    # Banned words
    banned_words = config_manager.get('banned_words', [])
    print(f"🚫 Parole bannate: {len(banned_words)} configurate")
    if banned_words:
        print(f"   └─ Esempi: {', '.join(banned_words[:3])}")
        if len(banned_words) > 3:
            print(f"   └─ ... e altre {len(banned_words) - 3} parole")
    
    # OpenAI
    openai_available = hasattr(moderation, 'openai_client') and moderation.openai_client is not None
    print(f"🤖 OpenAI: {'✅ Disponibile' if openai_available else '❌ Non configurato'}")
    
    print("=" * 50)

def run_critical_tests(moderation, config_manager):
    """Esegue test sui casi critici problematici."""
    print("\n🚨 TEST CASI CRITICI:")
    print("=" * 50)
    
    critical_cases = [
        ("No", "Messaggio molto breve"),
        ("@PoloLaDotta ciao", "Whitelist critica"),
        ("Ciao a tutti🤗Diffidate da chi propone panieri in forma privata. Affidatevi all'unico canale ufficiale preposto alla vendita di panieri e riassunti.Qui sotto 👇 metto il link del canale dove iscriversi se volete panieri e riassunti https://t.me/panieririassunti", "SPAM CRITICO - Deve essere bloccato!"),
        ("panieri e riassunti", "Test parole bannate dirette"),
        ("Boh speriamo", "Colloquiale italiano"),
        ("Attendiamo il link gmeet", "Mix italiano-tecnico"),
        ("Hello everyone", "Inglese"),
    ]
    
    for i, (test_msg, description) in enumerate(critical_cases, 1):
        print(f"\n--- TEST CRITICO {i}: {description} ---")
        print(f"Messaggio: '{test_msg[:100]}{'...' if len(test_msg) > 100 else ''}'")
        test_message(moderation, config_manager, test_msg)
        print("\n" + "-" * 50)

def main():
    """Funzione principale del test interattivo."""
    
    print("🤖 TEST INTERATTIVO MODERAZIONE BOT - VERSIONE FINALE")
    print("=" * 60)
    print("Digita messaggi per vedere se passerebbero nel tuo bot.")
    print("🆕 CORREZIONI APPLICATE:")
    print("  🔧 Filtro banned words migliorato")
    print("  🔧 Rilevamento link Telegram + materiale")
    print("  🔧 Prompt OpenAI aggiornato per link esterni")
    print("  🔧 Controllo lingua con meno falsi positivi")
    print("\nComandi speciali:")
    print("  • 'quit' o 'exit' per uscire")
    print("  • 'config' per vedere la configurazione")
    print("  • 'critical' per test casi critici")
    print("  • 'help' per vedere questi comandi")
    print("=" * 60)
    
    # Carica configurazione (disabilita log su console per pulire l'output)
    load_dotenv()
    config_manager = ConfigManager()
    logger = LoggingConfigurator.setup_logging(disable_console=True)
    
    try:
        moderation = AdvancedModerationBotLogic(config_manager, logger)
        print("✅ Sistema di moderazione inizializzato correttamente")
        
        # Mostra configurazione di base
        show_config_info(config_manager, moderation)
        
    except Exception as e:
        print(f"❌ Errore inizializzazione: {e}")
        print("Controlla la configurazione e riprova.")
        return
    
    while True:
        try:
            # Input dall'utente
            message = input("\n💬 Digita un messaggio da testare: ").strip()
            
            # Comandi speciali
            if message.lower() in ['quit', 'exit', 'q']:
                print("👋 Ciao!")
                break
            elif message.lower() in ['config', 'cfg']:
                show_config_info(config_manager, moderation)
                continue
            elif message.lower() in ['critical', 'test']:
                run_critical_tests(moderation, config_manager)
                continue
            elif message.lower() in ['help', 'h']:
                print("\n📖 COMANDI DISPONIBILI:")
                print("  • Digita qualsiasi messaggio per testarlo")
                print("  • 'config' per vedere la configurazione attuale")
                print("  • 'critical' per test casi critici")
                print("  • 'quit' o 'exit' per uscire")
                print("  • 'help' per vedere questi comandi")
                continue
            elif not message:
                print("⚠️  Messaggio vuoto, riprova.")
                continue
            
            # Test del messaggio
            test_message(moderation, config_manager, message)
            
        except KeyboardInterrupt:
            print("\n\n👋 Interrotto dall'utente. Ciao!")
            break
        except Exception as e:
            print(f"\n❌ Errore durante il test: {e}")
            print("Riprova con un altro messaggio.")

if __name__ == "__main__":
    main()