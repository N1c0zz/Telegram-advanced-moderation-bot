```python
import logging
import os
import re
import functools
from typing import List, Dict, Any, Optional, Tuple

import unidecode
from openai import OpenAI, OpenAIError

from .config_manager import ConfigManager
# from .sheets_interface import GoogleSheetsManager # Non direttamente usato qui, ma passato al costruttore
from .cache_utils import MessageAnalysisCache

try:
    import langdetect
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False
    logging.getLogger(__name__).warning("Libreria langdetect non trovata. Il rilevamento della lingua sarà limitato.")


class AdvancedModerationBotLogic: # Rinominato per chiarezza rispetto a TelegramModerationBot
    """
    Contiene la logica di analisi e moderazione dei messaggi,
    inclusa la normalizzazione del testo, il controllo delle parole bannate,
    l'analisi della lingua e l'interazione con OpenAI.
    """
    def __init__(self, config_manager: ConfigManager, logger: logging.Logger): # Rimossa dipendenza da SheetsManager
        self.config_manager = config_manager
        self.logger = logger
        # self.sheets_manager = sheets_manager # Non usato direttamente in questa classe
        
        self.banned_words: List[str] = self.config_manager.get('banned_words', [])
        self.allowed_languages: List[str] = self.config_manager.get('allowed_languages', ["italian"])
        
        self.char_map: Dict[str, str] = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"}
        self.analysis_cache = MessageAnalysisCache(cache_size=1000)
        
        self.stats: Dict[str, Any] = {
            'total_messages_analyzed_by_openai': 0, # Rinominato per chiarezza
            'direct_filter_matches': 0, # Rinominato
            'ai_filter_violations': 0,  # Rinominato
            'openai_requests': 0,
            'openai_cache_hits': 0,
            # Le statistiche 'deleted_messages', 'edited_messages_detected', 'edited_messages_deleted'
            # sono più appropriate nella classe bot principale che gestisce le azioni Telegram.
        }
        
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            self.openai_client = OpenAI(api_key=api_key)
            self.logger.info("Client OpenAI inizializzato.")
        else:
            self.openai_client = None
            self.logger.warning("OPENAI_API_KEY non trovato. L'analisi AI non sarà disponibile.")

    def get_stats(self) -> Dict[str, Any]:
        """Restituisce le statistiche di analisi dei messaggi."""
        total_analyzed = self.stats['total_messages_analyzed_by_openai']
        cache_hits = self.stats['openai_cache_hits']
        
        return {
            **self.stats,
            'cache_size': len(self.analysis_cache.cache),
            'cache_hit_rate': (cache_hits / total_analyzed) if total_analyzed > 0 else 0,
        }

    def normalize_text(self, text: str) -> str:
        """
        Normalizza il testo per migliorare il rilevamento:
        rimuove accenti, markdown, emoji, caratteri speciali (tranne @),
        sostituisce numeri con lettere simili, e converte in minuscolo.
        """
        # Rimuovi formattazione Markdown comune
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)  # Grassetto
        text = re.sub(r'__(.*?)__', r'\1', text)      # Corsivo/Sottolineato
        text = re.sub(r'~~(.*?)~~', r'\1', text)      # Barrato
        text = re.sub(r'`(.*?)`', r'\1', text)        # Codice inline
        text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text) # Link [testo](url) -> testo

        # Rimuovi emoji e simboli specifici (lista estesa)
        emoji_pattern = re.compile(
            "["
            u"\U0001F600-\U0001F64F"  # emoticons
            u"\U0001F300-\U0001F5FF"  # symbols & pictographs
            u"\U0001F680-\U0001F6FF"  # transport & map symbols
            u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
            u"\U00002702-\U000027B0"  # Dingbats
            u"\U000024C2-\U0001F251" 
            u"\u2600-\u26FF"          # Simboli vari
            u"\u2700-\u27BF"          # Dingbats
            u"🔴🔵⚪⚫🟠🟡🟢🟣⚽⚾🥎🏀🏐🏈🏉🎱🪀🏓⚠️🚨🚫⛔️🆘🔔🔊📢📣" # Simboli specifici
            "]+", flags=re.UNICODE)
        text = emoji_pattern.sub('', text)

        text = unidecode.unidecode(text.lower()) # Minuscolo e rimuovi accenti
        text = re.sub(r"[^a-z0-9\s@]", "", text) # Rimuovi non alfanumerici (mantieni spazi e @)

        for char_from, char_to in self.char_map.items(): # Sostituisci numeri con lettere
            text = text.replace(char_from, char_to)
        
        text = re.sub(r'\s+', ' ', text).strip() # Normalizza spazi
        return text

    @functools.lru_cache(maxsize=500) # Cache per questa funzione specifica se chiamata spesso con lo stesso input
    def contains_banned_word(self, text: str) -> bool:
        """
        Verifica se il testo normalizzato contiene una delle parole/frasi bannate.
        Questo è un filtro ad alta confidenza per spam/vendite esplicite.
        """
        normalized_text = self.normalize_text(text)
        if not normalized_text: # Testo vuoto dopo normalizzazione
            return False

        # Pattern per vendita esplicita o termini di pagamento
        # NOTA: La lista `banned_words` da config.json è già stata normalizzata
        # e viene usata per un controllo letterale. Qui aggiungiamo pattern regex.
        high_confidence_patterns = [
            r"vendo\s.*\s[0-9]+\s*(?:euro|€)",
            r"offro\s.*\s[0-9]+\s*(?:euro|€)",
            r"a\s+soli\s+[0-9]+\s*(?:euro|€)",
            r"prezzo\s+(?:trattabile|privato|contattami|scrivimi)",
            r"pagamento\s+(?:anticipato|bonifico|paypal|postepay)",
            # Crypto scam keywords
            r"investimento\s+(?:sicuro|garantito|crypto|bitcoin)",
            r"guadagn\w+\s+(?:facile|online|subito|trading)",
            r"mining\s+pool",
            r"crypto\s+bot",
        ]

        for pattern in high_confidence_patterns:
            if re.search(pattern, normalized_text, re.IGNORECASE):
                self.logger.debug(f"Rilevato pattern bannato ad alta confidenza: '{pattern}' in '{normalized_text}'")
                return True
        
        # Controllo parole bannate da config.json (già normalizzate all'init)
        # Per un match più robusto, potremmo normalizzare anche le banned_words all'init.
        # Assumiamo che le parole in config siano già in formato "normalizzabile"
        for banned_phrase in self.banned_words:
            # Normalizza anche la frase bannata per un confronto corretto
            normalized_banned_phrase = self.normalize_text(banned_phrase) # Assicura che sia normalizzata come il testo
            if normalized_banned_phrase in normalized_text: # Cerca la frase esatta normalizzata
                 self.logger.debug(f"Rilevata frase bannata da config: '{normalized_banned_phrase}' in '{normalized_text}'")
                 return True
            # Considera anche una ricerca con \bword\b se le frasi sono singole parole
            if f" {normalized_banned_phrase} " in f" {normalized_text} ": # Match di parola intera
                 self.logger.debug(f"Rilevata parola bannata (word boundary) da config: '{normalized_banned_phrase}' in '{normalized_text}'")
                 return True


        return False

    def contains_suspicious_contact_invitation(self, text: str) -> bool:
        """
        Verifica se il testo contiene inviti a contatto privato potenzialmente sospetti,
        specialmente se abbinati a termini di vendita o offerta di materiale.
        """
        normalized_text = self.normalize_text(text)
        if not normalized_text: return False

        # Whitelist di contesti legittimi per contatti (es. gruppi di studio)
        # Questi pattern, se presenti, potrebbero rendere l'invito al contatto meno sospetto
        legitimate_contexts = [
            "grupp\w+\s+(?:studio|whatsapp|telegram)", "aggiung\w+\s+gruppo",
            "link\s+gruppo", "mandat\w+\s+numer\w+", "entrare\s+nel\s+gruppo"
        ]
        for legit_pattern in legitimate_contexts:
            if re.search(legit_pattern, normalized_text):
                # Se c'è un contesto legittimo E NON ci sono termini di vendita espliciti,
                # allora l'invito potrebbe essere OK.
                if not any(term in normalized_text for term in ["vendo", "offro", "prezzo", "pagamento", "€", "euro"]):
                    self.logger.debug(f"Invito al contatto in contesto legittimo: '{normalized_text}'")
                    return False # Meno probabile che sia spam commerciale

        # Parole chiave per canali di contatto
        contact_channels = ["whatsapp", "telegram", "instagram", "dm", "direct", "privato", "@\w+"] # @username
        # Parole chiave per azioni di contatto
        contact_actions = ["scriv\w+", "contatt\w+", "mand\w+", "invia\w+", "messaggi\w+"]
        # Materiali/Servizi spesso offerti in modo sospetto
        offered_items = [
            "panier\w+", "appunt\w+", "material\w+", "tesi", "esami", "soluzion\w+",
            "aiuto", "lezioni", "slides", "aggiornat\w+"
        ]

        # Verifica combinazioni sospette
        has_contact_channel = any(re.search(channel, normalized_text) for channel in contact_channels)
        has_contact_action = any(action in normalized_text for action in contact_actions)
        has_offered_item = any(item in normalized_text for item in offered_items)

        if (has_contact_channel or has_contact_action) and has_offered_item:
            self.logger.debug(f"Rilevato invito al contatto sospetto: '{normalized_text}'")
            return True
        
        # Ricerca di @username seguito da offerta
        if re.search(r"@\w+", normalized_text) and has_offered_item:
            self.logger.debug(f"Rilevato @username con offerta materiale: '{normalized_text}'")
            return True

        return False
        
    def detect_language(self, text: str) -> Optional[str]:
        """Rileva la lingua principale del messaggio usando langdetect."""
        if not LANGDETECT_AVAILABLE or not text or len(text.strip()) < 5: # Ignora testi troppo corti
            return None 
        try:
            # Langdetect può lanciare eccezioni su testi molto corti o ambigui
            # Forza la rilevazione di una sola lingua
            detected_langs = langdetect.detect_langs(text)
            if detected_langs:
                return detected_langs[0].lang # Prendi la lingua più probabile
            return None
        except langdetect.lang_detect_exception.LangDetectException:
            self.logger.warning(f"Langdetect non è riuscito a rilevare la lingua per: '{text[:50]}...'")
            return None # Fallback a non rilevato

    def is_language_disallowed(self, text: str) -> bool:
        """Determina se la lingua del messaggio non è tra quelle consentite."""
        if not self.allowed_languages or "any" in self.allowed_languages: # Se 'any' è consentito, nessuna lingua è vietata
            return False

        detected_lang_code = self.detect_language(text)
        if detected_lang_code:
            # Confronta il codice lingua rilevato (es. 'en', 'it') con la lista di lingue consentite
            # Le lingue consentite in config potrebbero essere nomi (es. "italian")
            # langdetect restituisce codici ISO 639-1 (es. "it")
            # È necessario mappare o usare codici ISO nella config.
            # Per semplicità, assumiamo che allowed_languages contenga codici ISO.
            if detected_lang_code not in self.allowed_languages:
                self.logger.info(f"Lingua non consentita rilevata: '{detected_lang_code}'. Consentite: {self.allowed_languages}")
                return True
            return False # Lingua rilevata ed è consentita

        # Fallback se langdetect non è disponibile o non rileva nulla:
        # Si potrebbe usare un semplice controllo basato su un piccolo dizionario di parole italiane comuni
        # ma è meno affidabile. Per ora, se non rilevato, lo consideriamo consentito per evitare falsi positivi.
        # Il tuo `is_primarily_non_italian` originale era un buon tentativo di fallback.
        # Lo integriamo qui come ultima spiaggia se langdetect fallisce.
        if not LANGDETECT_AVAILABLE and len(text.split()) >= 5: # Solo se langdetect non c'è e testo non troppo corto
            words = re.findall(r'\b\w+\b', text.lower())
            if not words: return False

            italian_common_words = {
                "il", "la", "lo", "i", "gli", "le", "un", "uno", "una", "di", "a", "da", "in", "con", "su", "per",
                "e", "o", "ma", "se", "che", "non", "si", "mi", "ti", "ci", "vi", "lo", "li", "ne",
                "sono", "sei", "è", "siamo", "siete", "hanno", "ho", "hai", "ha", "abbiamo", "avete",
                "ciao", "grazie", "prego", "come", "quando", "dove", "perché", "cosa", "chi",
                "università", "esame", "studio", "lezione", "professore", "crediti"
            } # Lista di esempio, da espandere per maggiore accuratezza
            
            italian_word_count = sum(1 for word in words if word in italian_common_words)
            # Se meno del 15% delle parole sono italiane comuni (e ci sono almeno 5 parole),
            # potrebbe essere un'altra lingua. Soglia da aggiustare.
            if len(words) > 0 and (italian_word_count / len(words)) < 0.15:
                self.logger.info(f"Fallback: lingua probabilmente non italiana basata su conteggio parole per '{text[:50]}...'")
                return True
        
        return False # Lingua consentita o non rilevabile con certezza

    def analyze_with_openai(self, message_text: str) -> Tuple[bool, bool, bool]:
        """
        Analizza il messaggio con OpenAI per determinare se è inappropriato,
        una domanda, o in una lingua non consentita.

        Restituisce: (is_inappropriate, is_question, is_disallowed_language)
        """
        if not self.openai_client:
            self.logger.warning("OpenAI client non disponibile. Analisi AI saltata.")
            # Fallback a controlli locali più semplici
            is_inappropriate_local = self.contains_banned_word(message_text) or \
                                     self.contains_suspicious_contact_invitation(message_text)
            is_disallowed_lang_local = self.is_language_disallowed(message_text)
            return is_inappropriate_local, False, is_disallowed_lang_local # Non possiamo determinare "domanda" localmente

        # Messaggi molto corti o solo simboli potrebbero non necessitare di analisi AI costosa
        if len(message_text.strip()) <= 10 or re.match(r'^[^\w\s]+$', message_text.strip()):
            # Potremmo comunque fare un controllo di lingua se LANGDETECT_AVAILABLE
            is_disallowed_lang_short = self.is_language_disallowed(message_text)
            return False, False, is_disallowed_lang_short

        self.stats['total_messages_analyzed_by_openai'] += 1
        
        cached_result = self.analysis_cache.get(message_text)
        if cached_result:
            self.stats['openai_cache_hits'] += 1
            self.logger.debug("Risultato analisi OpenAI dalla cache.")
            return cached_result

        self.stats['openai_requests'] += 1
        
        # Il tuo prompt è molto dettagliato e ben strutturato. Lo manteniamo.
        # IMPORTANTE: Questo prompt è molto lungo, verifica i costi e la latenza con OpenAI.
        # Potrebbe essere necessario ottimizzarlo o usare modelli più economici/veloci per alcuni task.
        system_prompt = (
            "Sei un moderatore esperto di un gruppo Telegram universitario italiano. Analizza ogni messaggio con attenzione e rispondi SOLO con questo formato:\n"
            "INAPPROPRIATO: SI/NO\n"
            "DOMANDA: SI/NO\n"
            "LINGUA: CONSENTITA/NON CONSENTITA\n\n"
            
            "⚠️ PRIORITÀ ASSOLUTA: EVITARE FALSI POSITIVI! CONSIDERA APPROPRIATO QUALSIASI MESSAGGIO CHE NON È CHIARAMENTE PROBLEMATICO.\n\n"

            "REGOLE PER ALFABETI NON LATINI:\n"
            "❌ Qualsiasi messaggio che contiene prevalentemente testo in cirillico o altri alfabeti non latini deve essere marcato come LINGUA: NON CONSENTITA.\n"
            "❌ Messaggi con @username seguiti da testo in cirillico sono sempre da considerare INAPPROPRIATO: SI\n"
            "❌ Messaggi con emoji + testo in cirillico sono sempre da considerare INAPPROPRIATO: SI\n"
            "❌ Annunci pubblicitari in qualsiasi lingua diversa dall'italiano sono sempre INAPPROPRIATO: SI\n\n"
            
            "PROCESSO DI ANALISI (da seguire in ordine):\n"
            "1. Verifica se il messaggio è completamente in lingua straniera\n"
            "2. Verifica se contiene insulti gravi diretti ad altri utenti\n"
            "3. Verifica se contiene offerte commerciali ESPLICITE con menzione di pagamenti\n"
            "4. Verifica se contiene promozioni di investimenti, trading o criptovalute\n"
            "5. Verifica se il messaggio è una DOMANDA (con o senza punto interrogativo)\n"
            "6. Se hai dubbi, considera il messaggio APPROPRIATO\n\n"
            
            "DETTAGLIO DEI CRITERI:\n\n"
            
            "1️⃣ LINGUA (analizza per prima cosa):\n"
            f"Lingue consentite (codici ISO 639-1): {self.allowed_languages}\n" # Inietta le lingue consentite nel prompt
            "❌ NON CONSENTITA: SOLO messaggi INTERAMENTE in lingua straniera (non tra quelle consentite) senza italiano\n"
            "    • Esempio non consentito (se solo 'it' è consentito): Hello everyone how are you today\n"
            "✅ CONSENTITA: Tutto il resto, incluso:\n"
            "    • Messaggi in italiano con alcune parole straniere\n"
            "    • Messaggi che citano o discutono lingue straniere\n"
            "    • Messaggi che contengono termini tecnici in inglese\n"
            "    • Messaggi con errori grammaticali o sintattici\n\n"
            
            "2️⃣ INAPPROPRIATO (solo questi casi specifici sono inappropriati):\n"
            "❌ Vendita ESPLICITA di materiale didattico con CHIARA menzione di denaro\n"
            "    • Vendo panieri a 20€, Offro appunti a pagamento, Materiale disponibile a 15€\n"
            "❌ Transazioni commerciali con termini ESPLICITI come:\n"
            "    • prezzo, costo, euro, €, pagamento, acquistare, vendere, comprare, soldi\n"
            "❌ Inviti a contattare privatamente SOLO SE accompagnati da termini commerciali:\n"
            "    • Scrivetemi in privato per acquistare, Contattatemi per prezzi\n"
            "❌ Link a canali esterni con espliciti intenti commerciali:\n"
            "    • Entra nel nostro canale per acquistare materiale a prezzi scontati\n"
            "❌ Insulti pesanti diretti ad altri utenti:\n"
            "    • Offese personali gravi, linguaggio d'odio, minacce\n"
            "❌ Promozioni di investimenti o trading:\n"
            "    • Messaggi che promuovono guadagni facili attraverso trading o investimenti\n"
            "    • Messaggi che promuovono esperti di trading/investimenti da contattare\n"
            "    • Promozioni di servizi di consulenza per investimenti o trading\n"
            "    • Offerte di guadagno attraverso criptovalute o forex\n"
            "    • Messaggi che condividono link a gruppi o bot per investimenti\n\n"
            
            "3️⃣ CASI SEMPRE APPROPRIATI (non marcare mai come inappropriati):\n"
            "✅ Richieste di materiale didattico tra studenti:\n"
            "    • Qualcuno ha i panieri di questo esame?, Avete gli appunti per Diritto Privato?\n"
            "✅ Richieste di aggiunta a gruppi di studio o scambio numeri per gruppi:\n"
            "    • Mandatemi i vostri numeri per il gruppo WhatsApp, Posso entrare nel gruppo di studio?\n"
            "✅ Scambio di contatti per GRUPPI DI STUDIO (mai marcare come inappropriato):\n"
            "    • Scrivetemi in privato per entrare nel gruppo, Vi aggiungo al gruppo WhatsApp\n"
            "✅ Discussioni accademiche legittime su economia, finanza o criptovalute\n"
            "✅ Lamentele sull'università o sui servizi didattici\n"
            "✅ Domande su esami, procedure burocratiche, certificati, date\n"
            "✅ Messaggi brevi, emoji, numeri di telefono, indirizzi email\n\n"
            "✅ Richieste di compilazione questionari o partecipazione a ricerche accademiche:\n"
            "    • Studenti che cercano partecipanti per tesi, ricerche o progetti universitari\n"
            "    • Link a Google Forms, SurveyMonkey, o altre piattaforme di sondaggi per scopi didattici\n"
            "    • Richieste di aiuto per raccolta dati o partecipazione a esperimenti accademici\n"
            "    • Link relativi a contenuti didattici o universitari come progetti di ricerca legittimi\n\n"
            "✅ Richieste legittime di panieri che NON sono offerte di vendita:\n"
            "    • Ciao a tutti, qualcuno ha i panieri aggiornati di diritto privato?\n"
            "    • Cerco i panieri aggiornati, qualcuno può aiutarmi?\n\n"

            "\nREGOLE SPECIALI PER LINK:\n"
            "✅ Link a gruppi WhatsApp sono SEMPRE appropriati se:\n"
            "    • Non contengono menzione esplicita di denaro o pagamenti\n"
            "    • Non promuovono vendita di materiale didattico\n"
            "    • Non contengono termini commerciali (prezzo, costo, euro, €, pagamento)\n"
            "\n⚠️ Esempi di link SEMPRE APPROPRIATI:\n"
            "    • https://chat.whatsapp.com/Ab12CdEfGhIj (semplice link senza testo aggiuntivo)\n"
            "    • Ecco il link: https://chat.whatsapp.com/Ab12CdEfGhIj\n\n"
            
            "ESEMPI DI MESSAGGI CHIARAMENTE INAPPROPRIATI (marca SI):\n"
            "- Vendo panieri aggiornati e materiale didattico a 30€, contattatemi in privato\n"
            "- Offro aiuto tesi a pagamento, scrivetemi su WhatsApp 123456789\n"
            "- Entra nella community! @panieriunipegasomercatorum - materiale a soli 15€\n"
            "- Ciao a tutti! Ho scoperto un sistema per guadagnare con il trading. Contattate @trader123 per info\n"
            "- Ciao a tutti😊 Se cercate panieri aggiornati e corretti con anche le risposte inedite di triennali e magistrali\n\n"
            
            "ESEMPI DI MESSAGGI TRUFFA CRYPTO/TRADING (marca SI):\n"
            "- Ho trovato qualcuno di cui mi fido per fare trading di criptovalute. Contattala direttamente\n"
            "- Grazie a @expert_trader ho aumentato i miei guadagni del 200%, contattatelo\n\n"

            "ESEMPI DI MESSAGGI DI VENDITA DI PANIERI MASCHERATI (marca SI):\n"
            "- Ciao a tutti😊 Se cercate panieri aggiornati e corretti contattarmi\n"
            "- Ciao ragazzi, chi cerca panieri completi 2025 mi scriva\n\n"
            
            "ESEMPI DI MESSAGGI AMBIGUI MA APPROPRIATI (marca NO):\n"
            "- Ciao a tutti! Sto lavorando alla mia tesi e cerco partecipanti per un questionario. Ecco il link: https://forms.gle...\n"
            "- Salve, sono uno studente di economia e sto conducendo una ricerca, qualcuno può compilare questo form? https://forms.gle...\n"
            "- Qualcuno può passarmi i panieri aggiornati?\n"
            "- Chi ha i panieri di questo esame? Ne avrei bisogno urgentemente\n"
            "- Per favore mandate i numeri così vi aggiungo al gruppo WhatsApp\n\n"
            
            "CONTESTO UNIVERSITÀ TELEMATICHE:\n"
            "I panieri sono raccolte legittime di domande d'esame. È normale che gli studenti se li scambino gratuitamente. Solo la VENDITA di panieri è inappropriata.\n\n"
            
            "IMPORTANTE: Se un messaggio non è CHIARAMENTE inappropriato secondo i criteri sopra, marcalo come APPROPRIATO. In caso di dubbio, è sempre meglio permettere un messaggio potenzialmente inappropriato piuttosto che bloccare un messaggio legittimo.\n\n"

            "ISTRUZIONI SPECIFICHE PER RICONOSCERE DOMANDE:\n"
            "Una domanda è un messaggio che richiede informazioni, chiarimenti, aiuto o conferma da altri utenti. Marca come DOMANDA: SI se:\n\n"
            
            "✅ CRITERI PER RICONOSCERE UNA DOMANDA:\n"
            "• Il messaggio contiene un punto interrogativo ?\n"
            "• Il messaggio inizia con parole interrogative: chi, cosa, come, dove, quando, perché, quale, quanto\n"
            "• Il messaggio chiede informazioni sulla piattaforma, accesso, corsi, esami, costi\n"
            "• Il messaggio richiede conferma con strutture come: 'qualcuno sa', 'c'è qualcuno', 'riuscite a', 'avete'\n"
            "• Il messaggio esprime una richiesta di aiuto o materiale\n"
            "• Il messaggio chiede opinioni o esperienze\n"
            "• Il messaggio usa il condizionale per chiedere informazioni: 'sapreste', 'potreste'\n"
            "• Il messaggio usa formule dirette come: 'mi serve sapere', 'cerco informazioni'\n\n"
            
            "ESEMPI DI DOMANDE DA RICONOSCERE CORRETTAMENTE (marca DOMANDA: SI):\n"
            "- oggi riuscite ad entrare in piattaforma pegaso?\n"
            "- Buongiorno quanto costa all inclusive se fatta al terzo anno?\n"
            "- C'è una rappresentante per lm77?\n"
            "- Qualcuno ha i panieri di storia medievale?\n"
            "- Sapete quando escono i risultati dell'esame di ieri?\n\n"
            
            "NON SONO DOMANDE (marca DOMANDA: NO):\n"
            "- Buongiorno a tutti\n"
            "- Ho superato l'esame finalmente!\n"
            "- Grazie mille per l'aiuto\n\n"
            
            "IMPORTANTE: Una domanda può essere formulata anche senza punto interrogativo, valuta il contesto e l'intento. Ogni richiesta di informazioni o aiuto è una domanda, anche se formulata come affermazione."
        )
        
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-3.5-turbo", # Considera "gpt-3.5-turbo-0125" per risposte più strutturate o più recenti.
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message_text}
                ],
                temperature=0.0, # Bassa temperatura per risposte più deterministiche/fattuali
                max_tokens=50,   # Aumentato leggermente per sicurezza, ma il formato è breve
                timeout=15       # Timeout ragionevole
            )
            
            result_text = response.choices[0].message.content.strip() if response.choices[0].message.content else ""
            self.logger.debug(f"Risposta OpenAI: '{result_text}' per messaggio: '{message_text[:50]}...'")

            # Parsing robusto della risposta
            is_inappropriate = "INAPPROPRIATO: SI" in result_text
            is_question = "DOMANDA: SI" in result_text
            is_disallowed_language = "LINGUA: NON CONSENTITA" in result_text
            
            if "INAPPROPRIATO: SI" in result_text or "LINGUA: NON CONSENTITA" in result_text :
                 self.stats['ai_filter_violations'] +=1

            analysis_tuple = (is_inappropriate, is_question, is_disallowed_language)
            self.analysis_cache.set(message_text, analysis_tuple)
            return analysis_tuple

        except OpenAIError as e:
            self.logger.error(f"Errore API OpenAI: {e}", exc_info=True)
            # Fallback a controlli locali in caso di errore OpenAI
            is_inappropriate_local = self.contains_banned_word(message_text) or \
                                     self.contains_suspicious_contact_invitation(message_text)
            is_disallowed_lang_local = self.is_language_disallowed(message_text)
            return is_inappropriate_local, False, is_disallowed_lang_local
        except Exception as e: # Altre eccezioni generiche
            self.logger.error(f"Errore imprevisto durante l'analisi OpenAI: {e}", exc_info=True)
            is_inappropriate_local = self.contains_banned_word(message_text) or \
                                     self.contains_suspicious_contact_invitation(message_text)
            is_disallowed_lang_local = self.is_language_disallowed(message_text)
            return is_inappropriate_local, False, is_disallowed_lang_local
```