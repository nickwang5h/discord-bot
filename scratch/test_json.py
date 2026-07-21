import re

class MockAdvancedNews:
    def _clean_json_response(self, text: str) -> str:
        """Helper to extract JSON from possible markdown formatting."""
        # Try to find JSON block in markdown
        match = re.search(r'```(?:json)?(.*?)```', text, re.DOTALL)
        if match:
            return match.group(1).strip()
        
        # Fallback: locate the JSON array or object boundaries
        text = text.strip()
        if text.startswith('[') and text.endswith(']'):
            return text
        if text.startswith('{') and text.endswith('}'):
            return text
            
        start_array = text.find('[')
        end_array = text.rfind(']')
        start_obj = text.find('{')
        end_obj = text.rfind('}')
        
        is_array = start_array != -1 and end_array != -1 and start_array < end_array
        is_obj = start_obj != -1 and end_obj != -1 and start_obj < end_obj
        
        if is_array and is_obj:
            if start_array < start_obj:
                return text[start_array:end_array+1]
            else:
                return text[start_obj:end_obj+1]
        elif is_array:
            return text[start_array:end_array+1]
        elif is_obj:
            return text[start_obj:end_obj+1]
            
        return text

an = MockAdvancedNews()

cases = [
    # Case 1: normal json string
    '[{"test": 1}]',
    # Case 2: markdown
    '```json\n[{"test": 2}]\n```',
    # Case 3: extra data
    'Here is the result:\n[{"test": 3}]\nHope it helps.',
    # Case 4: extra data with object
    'Result:\n{"test": 4}\nDone.',
]

for i, case in enumerate(cases):
    print(f"--- Case {i+1} ---")
    clean = an._clean_json_response(case)
    print(f"Original: {case!r}")
    print(f"Cleaned: {clean!r}")
    print()
