# vim: set noexpandtab ts=4 sw=4:
#

clean:
	python setup.py clean

clean-all:
	python setup.py clean --all

# Remove autogenerated python bytecode
cleanpy:
	find . -name \*.pyc -delete
	find . -name \*__pycache__ -delete
