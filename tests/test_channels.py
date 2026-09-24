from evomesh.channels import Output


def test_write_prints_text(capsys):
    output = Output()
    output.write("hello")
    assert capsys.readouterr().out == "hello\n"
