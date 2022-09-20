default:
	@echo "Make target 'debian-docker' explicitly."

client/dist_deb:
	(cd client; make debian-docker)

server/dist_deb:
	(cd server; make debian-docker)

debian-docker: client/dist_deb server/dist_deb
	mkdir -p dist_deb
	cp client/dist_deb/* dist_deb/
	cp server/dist_deb/* dist_deb/
	ls -l dist_deb/

clean:
	rm -rf dist_deb
	(cd client; make clean)
	(cd server; make clean)
