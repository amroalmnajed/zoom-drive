import sys
import docx

def read_docx(path):
    doc = docx.Document(path)
    for i, table in enumerate(doc.tables):
        print(f'Table {i}:')
        for j, row in enumerate(table.rows):
            row_data = [cell.text.strip() for cell in row.cells]
            print(f'  Row {j}: {row_data}')

read_docx(r'C:\Users\lenovo\Desktop\project\Main_Revision .docx')
