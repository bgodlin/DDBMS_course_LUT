from query_processor import app
import json_parser

if __name__ == '__main__':
    app.run(debug=True, threaded=True, port=json_parser.retrieve_port('qp'))
