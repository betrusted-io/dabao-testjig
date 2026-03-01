# dabao-testjig

Scripts run on the test jig for dabao testing

Manual testing:

`cd code/testjig`
`sudo su`
`python3 ./ci.py --run-test final-test`

Install into device for autostart:

`sudo ln -s /home/bunnie/code/testjig/testjig.service /etc/systemd/system/testjig.service`
`sudo systemctl daemon-reload`
`sudo systemctl enable testjig`
`sudo systemctl start testjig`

Reduce journal usage:

In /etc/systemd/journald.conf modify this entry:

`SystemMaxUse=200M`