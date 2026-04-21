from flask import Flask, request, jsonify
import sys
from io import StringIO
import traceback

app = Flask(__name__)

@app.route('/execute', methods=['POST'])
def execute_code():
    try:
        code = request.json.get('code', '')
        if not code:
            return jsonify({'error': 'No code provided'}), 400
        
        # Capture stdout and stderr
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        redirected_output = StringIO()
        redirected_error = StringIO()
        sys.stdout = redirected_output
        sys.stderr = redirected_error
        
        result = None
        error = None
        
        try:
            # Execute the code
            exec_globals = {}
            exec(code, exec_globals)
            result = redirected_output.getvalue()
        except Exception as e:
            error = traceback.format_exc()
        finally:
            # Restore stdout and stderr
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        
        if error:
            return jsonify({
                'success': False,
                'error': error,
                'stderr': redirected_error.getvalue()
            }), 200
        
        return jsonify({
            'success': True,
            'output': result,
            'stderr': redirected_error.getvalue()
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
