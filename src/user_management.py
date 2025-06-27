import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
import csv
import os


class UserManagementSystem:
    """
    Sistema di gestione utenti per la dashboard.
    Integra con CSVDataManager per operazioni CRUD su utenti bannati.
    """
    
    def __init__(self, logger: logging.Logger, csv_manager, config_manager):
        self.logger = logger
        self.csv_manager = csv_manager
        self.config_manager = config_manager
        
    def get_banned_users_detailed(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Restituisce lista dettagliata degli utenti bannati per la dashboard.
        Include informazioni aggiuntive come data di ban, motivo, etc.
        ORDINAMENTO FISSO: dal più recente al più vecchio.
        """
        try:
            banned_data = self.csv_manager.read_csv_data("banned_users")
            
            if not banned_data:
                return []
            
            # IMPORTANTE: Ordina per timestamp (più recente prima)
            def parse_ban_timestamp(ban_record):
                timestamp = ban_record.get('timestamp', '')
                try:
                    if timestamp:
                        if 'T' in timestamp:  # ISO format
                            return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        else:  # Altri formati
                            return datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                    return datetime.min  # Se non c'è timestamp, metti alla fine
                except Exception as e:
                    self.logger.warning(f"Errore parsing timestamp ban '{timestamp}': {e}")
                    return datetime.min
            
            # Ordina dal più recente al più vecchio
            sorted_banned = sorted(banned_data, key=parse_ban_timestamp, reverse=True)
            
            # Prendi solo i primi N (i più recenti)
            recent_banned = sorted_banned[:limit]
            
            # Arricchisci i dati con informazioni aggiuntive
            enriched_data = []
            for ban_record in recent_banned:
                enriched_record = {
                    'user_id': ban_record.get('user_id', 'N/A'),
                    'timestamp': ban_record.get('timestamp', 'N/A'),
                    'motivo': ban_record.get('motivo', 'Non specificato'),
                    'ban_date_formatted': self._format_timestamp(ban_record.get('timestamp')),
                    'days_since_ban': self._calculate_days_since_ban(ban_record.get('timestamp')),
                    'can_unban': True  # Per ora sempre True, in futuro potremmo aggiungere logica
                }
                enriched_data.append(enriched_record)
            
            self.logger.info(f"Restituiti {len(enriched_data)} utenti bannati (ordinati dal più recente)")
            return enriched_data
            
        except Exception as e:
            self.logger.error(f"Errore nel recupero utenti bannati dettagliati: {e}")
            return []
    
    def search_user_messages(self, user_id: int, limit: int = 100) -> Dict[str, Any]:
        """
        Cerca tutti i messaggi di un utente specifico nel database.
        Utile per la dashboard per vedere lo storico di un utente.
        """
        try:
            all_messages = self.csv_manager.read_csv_data("messages")
            user_messages = [msg for msg in all_messages if msg.get('user_id') == str(user_id)]
            
            # Statistiche utente
            total_messages = len(user_messages)
            approved_messages = len([msg for msg in user_messages if msg.get('approvato') == 'SI'])
            rejected_messages = len([msg for msg in user_messages if msg.get('approvato') == 'NO'])
            questions = len([msg for msg in user_messages if msg.get('domanda') == 'SI'])
            
            # Ultimi messaggi (limitati)
            recent_messages = user_messages[:limit] if user_messages else []
            
            # Gruppi in cui ha scritto
            groups = list(set([msg.get('group_name', 'Sconosciuto') for msg in user_messages]))
            
            return {
                'user_id': user_id,
                'total_messages': total_messages,
                'approved_messages': approved_messages,
                'rejected_messages': rejected_messages,
                'questions': questions,
                'approval_rate': (approved_messages / total_messages * 100) if total_messages > 0 else 0,
                'groups': groups,
                'recent_messages': recent_messages,
                'is_banned': self.csv_manager.is_user_banned(user_id)
            }
            
        except Exception as e:
            self.logger.error(f"Errore nella ricerca messaggi utente {user_id}: {e}")
            return {
                'user_id': user_id,
                'error': str(e),
                'total_messages': 0,
                'approved_messages': 0,
                'rejected_messages': 0,
                'questions': 0,
                'approval_rate': 0,
                'groups': [],
                'recent_messages': [],
                'is_banned': False
            }
    
    def get_user_activity_summary(self, days: int = 7) -> Dict[str, Any]:
        """
        Restituisce un riepilogo dell'attività degli utenti negli ultimi N giorni.
        Utile per dashboard analytics.
        """
        try:
            from datetime import timedelta
            
            cutoff_date = datetime.now() - timedelta(days=days)
            cutoff_iso = cutoff_date.isoformat()
            
            # Messaggi recenti
            all_messages = self.csv_manager.read_csv_data("messages")
            recent_messages = [
                msg for msg in all_messages 
                if msg.get('timestamp', '1970-01-01') >= cutoff_iso
            ]
            
            # Ban recenti
            all_bans = self.csv_manager.read_csv_data("banned_users")
            recent_bans = [
                ban for ban in all_bans 
                if ban.get('timestamp', '1970-01-01') >= cutoff_iso
            ]
            
            # Statistiche
            total_recent_messages = len(recent_messages)
            approved_recent = len([msg for msg in recent_messages if msg.get('approvato') == 'SI'])
            rejected_recent = len([msg for msg in recent_messages if msg.get('approvato') == 'NO'])
            
            # Utenti più attivi
            user_message_counts = {}
            for msg in recent_messages:
                user_id = msg.get('user_id', 'unknown')
                user_message_counts[user_id] = user_message_counts.get(user_id, 0) + 1
            
            top_active_users = sorted(
                user_message_counts.items(), 
                key=lambda x: x[1], 
                reverse=True
            )[:10]
            
            return {
                'period_days': days,
                'total_messages': total_recent_messages,
                'approved_messages': approved_recent,
                'rejected_messages': rejected_recent,
                'approval_rate': (approved_recent / total_recent_messages * 100) if total_recent_messages > 0 else 0,
                'total_bans': len(recent_bans),
                'top_active_users': top_active_users,
                'ban_rate': (len(recent_bans) / len(set([msg.get('user_id') for msg in recent_messages])) * 100) if recent_messages else 0
            }
            
        except Exception as e:
            self.logger.error(f"Errore nel calcolo attività utenti: {e}")
            return {
                'period_days': days,
                'error': str(e),
                'total_messages': 0,
                'approved_messages': 0,
                'rejected_messages': 0,
                'approval_rate': 0,
                'total_bans': 0,
                'top_active_users': [],
                'ban_rate': 0
            }
    
    def get_moderation_insights(self) -> Dict[str, Any]:
        """
        Analizza i pattern di moderazione per fornire insights alla dashboard.
        """
        try:
            all_messages = self.csv_manager.read_csv_data("messages")
            all_bans = self.csv_manager.read_csv_data("banned_users")
            
            # Motivi di rifiuto più comuni
            rejection_reasons = {}
            rejected_messages = [msg for msg in all_messages if msg.get('approvato') == 'NO']
            
            for msg in rejected_messages:
                reason = msg.get('motivo_rifiuto', 'Non specificato')
                # Semplifica i motivi raggruppandoli
                simplified_reason = self._simplify_rejection_reason(reason)
                rejection_reasons[simplified_reason] = rejection_reasons.get(simplified_reason, 0) + 1
            
            # Motivi di ban più comuni
            ban_reasons = {}
            for ban in all_bans:
                reason = ban.get('motivo', 'Non specificato')
                simplified_reason = self._simplify_ban_reason(reason)
                ban_reasons[simplified_reason] = ban_reasons.get(simplified_reason, 0) + 1
            
            # Gruppi con più moderazione
            group_stats = {}
            for msg in all_messages:
                group_name = msg.get('group_name', 'Sconosciuto')
                if group_name not in group_stats:
                    group_stats[group_name] = {'total': 0, 'rejected': 0}
                
                group_stats[group_name]['total'] += 1
                if msg.get('approvato') == 'NO':
                    group_stats[group_name]['rejected'] += 1
            
            # Calcola tasso di rifiuto per gruppo
            for group in group_stats:
                total = group_stats[group]['total']
                rejected = group_stats[group]['rejected']
                group_stats[group]['rejection_rate'] = (rejected / total * 100) if total > 0 else 0
            
            # Ordina gruppi per tasso di rifiuto
            groups_by_rejection = sorted(
                group_stats.items(),
                key=lambda x: x[1]['rejection_rate'],
                reverse=True
            )[:10]
            
            # Analisi temporale (ultimi 30 giorni)
            from datetime import timedelta
            thirty_days_ago = (datetime.now() - timedelta(days=30)).isoformat()
            
            recent_messages = [
                msg for msg in all_messages 
                if msg.get('timestamp', '1970-01-01') >= thirty_days_ago
            ]
            
            recent_rejections = [msg for msg in recent_messages if msg.get('approvato') == 'NO']
            
            return {
                'total_messages_analyzed': len(all_messages),
                'total_rejections': len(rejected_messages),
                'overall_rejection_rate': (len(rejected_messages) / len(all_messages) * 100) if all_messages else 0,
                'total_bans': len(all_bans),
                'top_rejection_reasons': sorted(rejection_reasons.items(), key=lambda x: x[1], reverse=True)[:10],
                'top_ban_reasons': sorted(ban_reasons.items(), key=lambda x: x[1], reverse=True)[:10],
                'groups_by_rejection_rate': groups_by_rejection,
                'recent_30_days': {
                    'total_messages': len(recent_messages),
                    'total_rejections': len(recent_rejections),
                    'rejection_rate': (len(recent_rejections) / len(recent_messages) * 100) if recent_messages else 0
                }
            }
            
        except Exception as e:
            self.logger.error(f"Errore nel calcolo insights moderazione: {e}")
            return {
                'error': str(e),
                'total_messages_analyzed': 0,
                'total_rejections': 0,
                'overall_rejection_rate': 0,
                'total_bans': 0,
                'top_rejection_reasons': [],
                'top_ban_reasons': [],
                'groups_by_rejection_rate': [],
                'recent_30_days': {
                    'total_messages': 0,
                    'total_rejections': 0,
                    'rejection_rate': 0
                }
            }
    
    def bulk_unban_users(self, user_ids: List[int], reason: str = "Unban di massa da dashboard") -> Dict[str, Any]:
        """
        Sbanna multipli utenti (logicamente dal CSV).
        
        Args:
            user_ids: Lista di user ID da sbannare
            reason: Motivo dell'unban di massa
            
        Returns:
            Dict con risultati dell'operazione
        """
        try:
            results = {'success': [], 'failed': [], 'not_banned': []}
            
            for user_id in user_ids:
                if not self.csv_manager.is_user_banned(user_id):
                    results['not_banned'].append(user_id)
                    continue
                
                success = self.csv_manager.unban_user(
                    user_id=user_id,
                    unban_reason=reason,
                    unbanned_by="dashboard_bulk"
                )
                
                if success:
                    results['success'].append(user_id)
                else:
                    results['failed'].append(user_id)
            
            self.logger.info(
                f"Unban di massa completato: "
                f"{len(results['success'])} successi, "
                f"{len(results['failed'])} falliti, "
                f"{len(results['not_banned'])} non erano bannati"
            )
            
            return {
                'total_requested': len(user_ids),
                'successful_unbans': len(results['success']),
                'failed_unbans': len(results['failed']),
                'not_banned_count': len(results['not_banned']),
                'success_rate': len(results['success']) / len(user_ids) * 100 if user_ids else 0,
                'details': results,
                'message': f"Unban di massa: {len(results['success'])}/{len(user_ids)} completati con successo"
            }
            
        except Exception as e:
            self.logger.error(f"Errore durante unban di massa: {e}")
            return {
                'total_requested': len(user_ids),
                'successful_unbans': 0,
                'failed_unbans': len(user_ids),
                'not_banned_count': 0,
                'success_rate': 0,
                'details': {'success': [], 'failed': user_ids, 'not_banned': []},
                'message': f"Errore durante unban di massa: {str(e)}"
            }
    
    def get_unban_statistics(self) -> Dict[str, Any]:
        """
        Restituisce statistiche sugli unban per la dashboard.
        """
        try:
            unban_history = self.csv_manager.get_unban_history()
            
            if not unban_history:
                return {
                    'total_unbans': 0,
                    'unbans_last_7_days': 0,
                    'unbans_last_30_days': 0,
                    'top_unban_reasons': [],
                    'unban_trend': []
                }
            
            from datetime import timedelta
            now = datetime.now()
            seven_days_ago = now - timedelta(days=7)
            thirty_days_ago = now - timedelta(days=30)
            
            # Conta unban per periodo
            unbans_7d = 0
            unbans_30d = 0
            unban_reasons = {}
            
            for unban in unban_history:
                unban_date_str = unban.get('unban_timestamp', '')
                try:
                    unban_date = datetime.fromisoformat(unban_date_str.replace('Z', '+00:00'))
                    unban_date = unban_date.replace(tzinfo=None)  # Remove timezone for comparison
                    
                    if unban_date >= seven_days_ago:
                        unbans_7d += 1
                    if unban_date >= thirty_days_ago:
                        unbans_30d += 1
                        
                except ValueError:
                    continue
                
                # Conta motivi
                reason = unban.get('unban_reason', 'Non specificato')
                unban_reasons[reason] = unban_reasons.get(reason, 0) + 1
            
            # Top motivi unban
            top_reasons = sorted(unban_reasons.items(), key=lambda x: x[1], reverse=True)[:5]
            
            return {
                'total_unbans': len(unban_history),
                'unbans_last_7_days': unbans_7d,
                'unbans_last_30_days': unbans_30d,
                'top_unban_reasons': top_reasons,
                'recent_unbans': unban_history[:10]  # Ultimi 10 unban
            }
            
        except Exception as e:
            self.logger.error(f"Errore calcolo statistiche unban: {e}")
            return {
                'total_unbans': 0,
                'unbans_last_7_days': 0,
                'unbans_last_30_days': 0,
                'top_unban_reasons': [],
                'recent_unbans': [],
                'error': str(e)
            }
    
    def export_user_data(self, user_id: int, format: str = 'json') -> Optional[str]:
        """
        Esporta tutti i dati di un utente specifico per GDPR compliance.
        """
        try:
            user_data = self.search_user_messages(user_id)
            
            if format.lower() == 'json':
                import json
                return json.dumps(user_data, indent=2, ensure_ascii=False)
            elif format.lower() == 'csv':
                # TODO: Implementare export CSV
                return None
            else:
                return None
                
        except Exception as e:
            self.logger.error(f"Errore export dati utente {user_id}: {e}")
            return None
    
    def get_message_statistics_by_timeframe(self, timeframe: str = 'daily') -> List[Dict[str, Any]]:
        """
        Restituisce statistiche dei messaggi raggruppate per timeframe.
        Utile per grafici nella dashboard.
        """
        try:
            all_messages = self.csv_manager.read_csv_data("messages")
            
            # Raggruppa per data
            stats_by_date = {}
            
            for msg in all_messages:
                timestamp_str = msg.get('timestamp', '')
                if not timestamp_str:
                    continue
                
                try:
                    msg_date = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    
                    if timeframe == 'daily':
                        date_key = msg_date.strftime('%Y-%m-%d')
                    elif timeframe == 'weekly':
                        # Inizio della settimana (lunedì)
                        from datetime import timedelta
                        week_start = msg_date - timedelta(days=msg_date.weekday())
                        date_key = week_start.strftime('%Y-%m-%d')
                    elif timeframe == 'monthly':
                        date_key = msg_date.strftime('%Y-%m')
                    else:
                        date_key = msg_date.strftime('%Y-%m-%d')
                    
                    if date_key not in stats_by_date:
                        stats_by_date[date_key] = {
                            'date': date_key,
                            'total': 0,
                            'approved': 0,
                            'rejected': 0,
                            'questions': 0
                        }
                    
                    stats_by_date[date_key]['total'] += 1
                    
                    if msg.get('approvato') == 'SI':
                        stats_by_date[date_key]['approved'] += 1
                    elif msg.get('approvato') == 'NO':
                        stats_by_date[date_key]['rejected'] += 1
                    
                    if msg.get('domanda') == 'SI':
                        stats_by_date[date_key]['questions'] += 1
                        
                except Exception as date_error:
                    self.logger.warning(f"Errore parsing timestamp '{timestamp_str}': {date_error}")
                    continue
            
            # Converti in lista ordinata per data
            sorted_stats = sorted(stats_by_date.values(), key=lambda x: x['date'])
            
            # Aggiungi percentuali
            for stat in sorted_stats:
                total = stat['total']
                if total > 0:
                    stat['approval_rate'] = round(stat['approved'] / total * 100, 2)
                    stat['rejection_rate'] = round(stat['rejected'] / total * 100, 2)
                    stat['question_rate'] = round(stat['questions'] / total * 100, 2)
                else:
                    stat['approval_rate'] = 0
                    stat['rejection_rate'] = 0
                    stat['question_rate'] = 0
            
            return sorted_stats
            
        except Exception as e:
            self.logger.error(f"Errore nel calcolo statistiche per timeframe {timeframe}: {e}")
            return []
    
    # --- Metodi helper privati ---
    
    def _format_timestamp(self, timestamp_str: str) -> str:
        """Formatta timestamp per visualizzazione user-friendly."""
        if not timestamp_str:
            return 'Data sconosciuta'
        
        try:
            dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            return dt.strftime('%d/%m/%Y %H:%M')
        except Exception:
            return timestamp_str
    
    def _calculate_days_since_ban(self, timestamp_str: str) -> int:
        """Calcola giorni trascorsi dal ban."""
        if not timestamp_str:
            return 0
        
        try:
            ban_date = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            delta = datetime.now() - ban_date.replace(tzinfo=None)
            return delta.days
        except Exception:
            return 0
    
    def _simplify_rejection_reason(self, reason: str) -> str:
        """Semplifica i motivi di rifiuto per raggruppamento."""
        reason_lower = reason.lower()
        
        if 'parole' in reason_lower or 'bannati' in reason_lower or 'pattern' in reason_lower:
            return 'Contenuto Vietato'
        elif 'lingua' in reason_lower:
            return 'Lingua Non Consentita'
        elif 'ai' in reason_lower or 'inappropriato' in reason_lower:
            return 'Contenuto Inappropriato (AI)'
        elif 'spam' in reason_lower:
            return 'Spam Cross-Gruppo'
        elif 'utente bannato' in reason_lower:
            return 'Utente Già Bannato'
        elif 'night mode' in reason_lower or 'notturna' in reason_lower:
            return 'Modalità Notturna'
        else:
            return 'Altro'
    
    def _simplify_ban_reason(self, reason: str) -> str:
        """Semplifica i motivi di ban per raggruppamento."""
        reason_lower = reason.lower()
        
        if 'primo messaggio' in reason_lower:
            return 'Primo Messaggio Inappropriato'
        elif 'edit' in reason_lower or 'modificato' in reason_lower:
            return 'Messaggio Modificato Inappropriato'
        elif 'spam' in reason_lower:
            return 'Spam Cross-Gruppo'
        elif 'manuale' in reason_lower:
            return 'Ban Manuale Admin'
        elif 'lingua' in reason_lower:
            return 'Lingua Non Consentita'
        else:
            return 'Altro'


class SystemPromptManager:
    """
    Gestisce il system prompt per OpenAI dalla dashboard.
    """
    
    def __init__(self, logger: logging.Logger, moderation_logic):
        self.logger = logger
        self.moderation_logic = moderation_logic  # Può essere None
        self.prompt_file = "config/system_prompt.txt"
        
        # Crea directory se non esiste
        os.makedirs(os.path.dirname(self.prompt_file), exist_ok=True)

    def get_current_prompt(self) -> str:
        """Restituisce il prompt di sistema attuale (versione sicura)."""
        try:
            if os.path.exists(self.prompt_file):
                with open(self.prompt_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    return content if content else self._get_default_prompt()
            else:
                # Restituisce il prompt hardcoded da moderation_rules.py
                return self._get_default_prompt()
        except Exception as e:
            self.logger.error(f"Errore lettura system prompt: {e}")
            return self._get_default_prompt()
    
    def update_prompt(self, new_prompt: str) -> bool:
        """Aggiorna il system prompt."""
        try:
            # Valida che il prompt non sia vuoto
            if not new_prompt.strip():
                self.logger.error("Tentativo di salvare prompt vuoto")
                return False
            
            # Salva su file
            with open(self.prompt_file, 'w', encoding='utf-8') as f:
                f.write(new_prompt.strip())
            
            # Aggiorna il prompt nella logica di moderazione
            if self.moderation_logic and hasattr(self.moderation_logic, 'prompt_manager'):
            # Il prompt viene aggiornato automaticamente al prossimo utilizzo
                pass
            
            self.logger.info("System prompt aggiornato con successo")
            return True
            
        except Exception as e:
            self.logger.error(f"Errore aggiornamento system prompt: {e}")
            return False
    
    def reset_to_default(self) -> bool:
        """Reset del prompt alle impostazioni predefinite."""
        try:
            default_prompt = self._get_default_prompt()
            return self.update_prompt(default_prompt)
        except Exception as e:
            self.logger.error(f"Errore reset prompt a default: {e}")
            return False
    
    def _get_default_prompt(self) -> str:
        """Restituisce il prompt predefinito hardcoded."""
        # Questo è estratto da moderation_rules.py
        return """Sei un moderatore esperto di un gruppo Telegram universitario italiano. Analizza ogni messaggio con attenzione e rispondi SOLO con questo formato:\n"
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
                "5. **NUOVO**: Verifica se contiene link a canali esterni per vendita/offerta materiale didattico\n"
                "6. Verifica se il messaggio è una DOMANDA (con o senza punto interrogativo)\n"
                "7. Se hai dubbi, considera il messaggio APPROPRIATO\n\n"
                
                "DETTAGLIO DEI CRITERI:\n\n"
                
                "1️⃣ LINGUA (analizza per prima cosa):\n"
                f"Lingue consentite (codici ISO 639-1): {self.allowed_languages}\n"
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
                "❌ **NUOVO CRITICO**: Link a canali esterni Telegram per vendita/offerta materiale didattico:\n"
                "    • Qualsiasi messaggio che contiene link t.me/canale + offerta di panieri/riassunti/materiale\n"
                "    • Messaggi che promuovono 'canali ufficiali' per vendita materiale didattico\n"
                "    • Inviti a iscriversi a canali esterni per ottenere materiale didattico\n"
                "    • Esempi: 'Iscrivetevi al canale t.me/panieri', 'Materiale disponibile su t.me/riassunti'\n"
                "    • 'Affidatevi all'unico canale preposto alla vendita di panieri'\n"
                "❌ Insulti pesanti diretti ad altri utenti:\n"
                "    • Offese personali gravi, linguaggio d'odio, minacce\n"
                "❌ Promozioni di investimenti o trading:\n"
                "    • Messaggi che promuovono guadagni facili attraverso trading o investimenti\n"
                "    • Messaggi che promuovono esperti di trading/investimenti da contattare\n"
                "    • Promozioni di servizi di consulenza per investimenti o trading\n"
                "    • Offerte di guadagno attraverso criptovalute o forex\n"
                "    • Messaggi che condividono link a gruppi o bot per investimenti\n\n"
                
                "ATTENZIONE SPAM MASCHERATO DI PANIERI (SEMPRE INAPPROPRIATO):\n"
                "❌ Qualsiasi messaggio che invita al contatto privato per panieri/materiale È SEMPRE INAPPROPRIATO, anche senza menzione di prezzo:\n"
                "    • Ciao, chi cerca panieri aggiornati mi scriva\n"
                "    • Ho materiale completo, contattatemi\n" 
                "    • Panieri 2024 disponibili, interessati in privato\n"
                "    • Chi vuole i panieri mi contatti\n"
                "    • Ho tutto il materiale, scrivetemi\n"
                "    • Panieri completi, contattatemi per info\n"
                "❌ REGOLA: Se qualcuno offre panieri/materiale E chiede di essere contattato privatamente = INAPPROPRIATO: SI\n"
                "❌ Anche frasi come 'mi scriva', 'contattatemi', 'interessati in privato' sono SEMPRE sospette se legate a panieri\n\n"
                
                "❌ **NUOVA REGOLA CRITICA - LINK A CANALI ESTERNI**:\n"
                "❌ Qualsiasi messaggio che contiene link a canali Telegram esterni (t.me/*, telegram.me/*) combinato con:\n"
                "    • Offerta di materiale didattico (panieri, riassunti, appunti, slides, etc.)\n"
                "    • Inviti a iscriversi per ottenere materiale\n"
                "    • Promozione di 'canali ufficiali' per materiale\n"
                "    • È SEMPRE INAPPROPRIATO: SI, anche se non menziona prezzi esplicitamente\n"
                "❌ Esempi SEMPRE inappropriati:\n"
                "    • 'Iscrivetevi al canale https://t.me/panieri per materiale aggiornato'\n"
                "    • 'Affidatevi all'unico canale ufficiale preposto alla vendita di panieri t.me/riassunti'\n"
                "    • 'Qui sotto il link del canale dove iscriversi se volete panieri https://t.me/materiale'\n"
                "    • Qualsiasi variazione che combina link esterni + materiale didattico\n\n"
                
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
                "❌ Link a canali Telegram esterni (t.me/*, telegram.me/*) sono INAPPROPRIATI se:\n"
                "    • Combinati con offerta di materiale didattico\n"
                "    • Presentati come 'canali ufficiali' per materiale\n"
                "    • Contengono inviti a iscriversi per ottenere panieri/riassunti/materiale\n"
                "\n⚠️ Esempi di link SEMPRE APPROPRIATI:\n"
                "    • https://chat.whatsapp.com/Ab12CdEfGhIj (semplice link senza testo aggiuntivo)\n"
                "    • Ecco il link: https://chat.whatsapp.com/Ab12CdEfGhIj\n"
                "⚠️ Esempi di link SEMPRE INAPPROPRIATI:\n"
                "    • https://t.me/panieri + qualsiasi menzione di materiale didattico\n"
                "    • t.me/riassunti + invito a iscriversi per materiale\n\n"
                
                "ESEMPI DI MESSAGGI CHIARAMENTE INAPPROPRIATI (marca SI):\n"
                "- Vendo panieri aggiornati e materiale didattico a 30€, contattatemi in privato\n"
                "- Offro aiuto tesi a pagamento, scrivetemi su WhatsApp 123456789\n"
                "- Entra nella community! @panieriunipegasomercatorum - materiale a soli 15€\n"
                "- Ciao a tutti! Ho scoperto un sistema per guadagnare con il trading. Contattate @trader123 per info\n"
                "- Ciao a tutti😊 Se cercate panieri aggiornati e corretti con anche le risposte inedite di triennali e magistrali\n"
                "- **NUOVO**: Affidatevi all'unico canale ufficiale preposto alla vendita di panieri https://t.me/panieri\n"
                "- **NUOVO**: Qui sotto il link del canale dove iscriversi se volete panieri https://t.me/materiale\n\n"
                
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
                "I panieri sono raccolte legittime di domande d'esame. È normale che gli studenti se li scambino gratuitamente. Solo la VENDITA di panieri o la promozione di canali esterni per materiale è inappropriata.\n\n"
                
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
                
                "IMPORTANTE: Una domanda può essere formulata anche senza punto interrogativo, valuta il contesto e l'intento. Ogni richiesta di informazioni o aiuto è una domanda, anche se formulata come affermazione."""


class ConfigurationManager:
    """
    Gestisce le configurazioni modificabili dalla dashboard.
    """
    
    def __init__(self, config_manager, logger: logging.Logger):
        self.config_manager = config_manager
        self.logger = logger
    
    def get_editable_config(self) -> Dict[str, Any]:
        """Restituisce le configurazioni modificabili dalla dashboard (versione sicura)."""
        try:
            config = self.config_manager.config
        except Exception as e:
            self.logger.error(f"Errore accesso config: {e}")
            config = {}
        
        # Assicurati che tutte le chiavi abbiano valori di default sicuri
        safe_config = {
            'banned_words': config.get('banned_words', []),
            'whitelist_words': config.get('whitelist_words', []),
            'exempt_users': config.get('exempt_users', []),
            'allowed_languages': config.get('allowed_languages', ['it']),
            'auto_approve_short_messages': config.get('auto_approve_short_messages', True),
            'short_message_max_length': config.get('short_message_max_length', 4),
            'first_messages_threshold': config.get('first_messages_threshold', 3),
            'rules_command_enabled': config.get('rules_command_enabled', True),
            'rules_message': config.get('rules_message', ''),
        }
        
        # Night mode con controlli sicuri
        night_mode_config = config.get('night_mode', {})
        if isinstance(night_mode_config, dict):
            safe_config['night_mode'] = {
                'enabled': night_mode_config.get('enabled', True),
                'start_hour': night_mode_config.get('start_hour', '23:00'),
                'end_hour': night_mode_config.get('end_hour', '07:00'),
                'night_mode_groups': night_mode_config.get('night_mode_groups', []),
                'grace_period_seconds': night_mode_config.get('grace_period_seconds', 15)
            }
        else:
            safe_config['night_mode'] = {
                'enabled': True,
                'start_hour': '23:00',
                'end_hour': '07:00',
                'night_mode_groups': [],
                'grace_period_seconds': 15
            }
        
        # Spam detector con controlli sicuri
        spam_config = config.get('spam_detector', {})
        if isinstance(spam_config, dict):
            safe_config['spam_detector'] = {
                'time_window_hours': spam_config.get('time_window_hours', 1),
                'similarity_threshold': spam_config.get('similarity_threshold', 0.85),
                'min_groups': spam_config.get('min_groups', 2)
            }
        else:
            safe_config['spam_detector'] = {
                'time_window_hours': 1,
                'similarity_threshold': 0.85,
                'min_groups': 2
            }
        
        return safe_config
    
    def update_config_section(self, section: str, new_values: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Aggiorna una sezione specifica della configurazione.
        Restituisce (success, message).
        """
        try:
            config = self.config_manager.config.copy()
            
            if section == 'banned_words':
                if not isinstance(new_values.get('banned_words'), list):
                    return False, "banned_words deve essere una lista"
                config['banned_words'] = new_values['banned_words']
                
            elif section == 'whitelist_words':
                if not isinstance(new_values.get('whitelist_words'), list):
                    return False, "whitelist_words deve essere una lista"
                config['whitelist_words'] = new_values['whitelist_words']
                
            elif section == 'exempt_users':
                if not isinstance(new_values.get('exempt_users'), list):
                    return False, "exempt_users deve essere una lista"
                config['exempt_users'] = new_values['exempt_users']
                
            elif section == 'night_mode':
                night_mode_config = config.get('night_mode', {})
                night_mode_config.update(new_values)
                config['night_mode'] = night_mode_config
                
            elif section == 'spam_detector':
                spam_config = config.get('spam_detector', {})
                spam_config.update(new_values)
                config['spam_detector'] = spam_config
                
            else:
                # Aggiornamento generico
                config.update(new_values)
            
            # Salva la configurazione
            self.config_manager.save_config(config)
            self.config_manager.config = config
            
            self.logger.info(f"Configurazione sezione '{section}' aggiornata da dashboard")
            return True, f"Sezione '{section}' aggiornata con successo"
            
        except Exception as e:
            self.logger.error(f"Errore aggiornamento configurazione sezione '{section}': {e}")
            return False, f"Errore: {str(e)}"
    
    def validate_config_changes(self, section: str, new_values: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Valida le modifiche alla configurazione prima di applicarle.
        Restituisce (is_valid, list_of_errors).
        """
        errors = []
        
        try:
            if section == 'night_mode':
                # Valida formato orari
                start_hour = new_values.get('start_hour', '')
                end_hour = new_values.get('end_hour', '')
                
                if start_hour:
                    try:
                        datetime.strptime(start_hour, '%H:%M')
                    except ValueError:
                        errors.append(f"Formato start_hour non valido: {start_hour}. Usare HH:MM")
                
                if end_hour:
                    try:
                        datetime.strptime(end_hour, '%H:%M')
                    except ValueError:
                        errors.append(f"Formato end_hour non valido: {end_hour}. Usare HH:MM")
                
                # Valida gruppi
                groups = new_values.get('night_mode_groups', [])
                if not isinstance(groups, list):
                    errors.append("night_mode_groups deve essere una lista")
                else:
                    for group_id in groups:
                        if not isinstance(group_id, int):
                            errors.append(f"ID gruppo non valido: {group_id}. Deve essere un numero intero")
            
            elif section == 'spam_detector':
                threshold = new_values.get('similarity_threshold')
                if threshold is not None:
                    if not (0.0 <= threshold <= 1.0):
                        errors.append("similarity_threshold deve essere tra 0.0 e 1.0")
                
                time_window = new_values.get('time_window_hours')
                if time_window is not None:
                    if not isinstance(time_window, (int, float)) or time_window <= 0:
                        errors.append("time_window_hours deve essere un numero positivo")
            
            # Aggiungi altre validazioni per altre sezioni se necessario
            
        except Exception as e:
            errors.append(f"Errore durante validazione: {str(e)}")
        
        return len(errors) == 0, errors
    
    def backup_current_config(self) -> str:
        """Crea un backup della configurazione corrente."""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_filename = f"config_backup_{timestamp}.json"
            backup_path = os.path.join("config", "backups", backup_filename)
            
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            
            import json
            with open(backup_path, 'w', encoding='utf-8') as f:
                json.dump(self.config_manager.config, f, indent=2, ensure_ascii=False)
            
            self.logger.info(f"Backup configurazione creato: {backup_path}")
            return backup_path
            
        except Exception as e:
            self.logger.error(f"Errore creazione backup configurazione: {e}")
            return ""